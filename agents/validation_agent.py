import ast
import json
from pathlib import Path

import whatthepatch
from agents.llm import get_client, MODEL, debug_response
from state import PipelineState

_SYSTEM = (
    "You are a code review expert. "
    "Respond ONLY with valid JSON — no prose, no markdown fences."
)

_USER = """\
Review whether the patched file fully resolves the SonarQube issues listed below without introducing regressions.

Issues that must be resolved:
{issues}

Patched file content:
```
{patched_code}
```

Return a JSON object:
{{"passed": true|false, "reason": "<concise explanation>"}}
"""


def _format_issues(issues: list[dict]) -> str:
    return "\n".join(
        f"- [{i.get('severity', '?')}] {i.get('rule', '?')}: "
        f"{i.get('action_message', '')} (line {i.get('start_line', '?')})"
        for i in issues
    )


def _apply_diff_to_temp(source: str, diff: str) -> tuple[bool, str, str]:
    """
    Apply diff to source in memory using whatthepatch.
    Returns (success, patched_content, error_message).
    """
    try:
        patches = list(whatthepatch.parse_patch(diff))
        if not patches:
            return False, "", "No patches found in diff"

        text = source
        for patch in patches:
            if not patch.changes:
                continue
            result = whatthepatch.apply_diff(patch, text)
            if result is None:
                return False, "", "Patch did not apply cleanly (context mismatch)"
            text = "".join(result)

        return True, text, ""
    except Exception as exc:
        return False, "", str(exc)


def _extract_function(source: str, language: str, fn_name: str) -> str:
    """Extract named function from source using tree-sitter. Falls back to full source."""
    from agents.context_agent import _LANGUAGE_CONFIG
    from tree_sitter import Parser

    cfg = _LANGUAGE_CONFIG.get(language)
    if cfg is None:
        return source

    source_bytes = source.encode("utf-8")
    parser = Parser(cfg["language"])
    tree = parser.parse(source_bytes)
    fn_node_types = cfg["function_nodes"]

    def walk(node):
        if node.type in fn_node_types:
            for child in node.children:
                if child.type == "identifier":
                    name = source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                    if name == fn_name:
                        return node
        for child in node.children:
            result = walk(child)
            if result is not None:
                return result
        return None

    node = walk(tree.root_node)
    if node is None:
        return source
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _syntax_check(patched_code: str, language: str) -> tuple[bool, str]:
    """
    Returns (ok, error_message).
    Python uses ast.parse (stdlib). Other languages use tree-sitter ERROR node detection.
    """
    if language == "python":
        try:
            ast.parse(patched_code)
            return True, ""
        except SyntaxError as e:
            return False, f"SyntaxError: {e}"

    from agents.context_agent import _LANGUAGE_CONFIG
    from tree_sitter import Parser

    cfg = _LANGUAGE_CONFIG.get(language)
    if cfg is None:
        return True, ""  # unknown language — skip

    parser = Parser(cfg["language"])
    tree = parser.parse(patched_code.encode("utf-8"))

    errors: list[str] = []

    def walk(node):
        if node.type == "ERROR":
            errors.append(f"line {node.start_point[0] + 1}")
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    if errors:
        return False, f"Parse errors at: {', '.join(errors)}"
    return True, ""


def _parse_json(text: str) -> dict:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


def validation_node(state: PipelineState) -> dict:
    remediation_status = state.get("remediation_status", "passed")
    ctx = state["context"]
    language = ctx.get("language", "unknown")

    # Pass-through: remediation decided no change was needed
    if remediation_status == "no_fix_needed":
        return {
            "validation": {
                "passed": True,
                "reason": state.get("remediation_reason", "no fix required"),
            },
            "patched_code": ctx["full_code"],
            "last_event": "validation_passed",
        }

    diff = state.get("diff")
    if not diff:
        return {
            "validation": {
                "passed": False,
                "reason": "No diff was produced by the Remediation Agent",
            },
            "last_event": "validation_failed",
        }

    issues = state["current_issues"]

    # Diffs use full-file line numbers, so apply against the full evolving file.
    current_code = state.get("current_code") or ctx["full_code"]

    print(f"[Validation] Applying patch to {ctx['file_path']}")

    # ── Phase 1: structural check (patch applies cleanly) ────
    ok, patched_code, err = _apply_diff_to_temp(current_code, diff)
    if not ok:
        print(f"[Validation] Patch failed to apply: {err.strip()}")
        return {
            "validation": {
                "passed": False,
                "reason": f"Patch did not apply cleanly: {err.strip()}",
            },
            "last_event": "validation_failed",
        }

    # Extract just the patched function for review (by name, not line slice)
    functions = ctx.get("functions", [])
    fn = functions[0] if functions else None
    if fn:
        patched_fn = _extract_function(patched_code, language, fn["name"])
    else:
        patched_fn = patched_code

    # ── Phase 0: syntax check (on patched function) ──────────
    print(f"[Validation] Running syntax check ({language})")
    syntax_ok, syntax_err = _syntax_check(patched_fn, language)
    if not syntax_ok:
        print(f"[Validation] Syntax check FAILED: {syntax_err}")
        return {
            "validation": {
                "passed": False,
                "reason": f"Syntax error in patched file: {syntax_err}",
            },
            "last_event": "validation_failed",
        }

    # ── Phase 2: semantic LLM review (on patched function) ───
    print("[Validation] Running semantic review")
    client = get_client()
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": _USER.format(
                    issues=_format_issues(issues),
                    patched_code=patched_fn,
                ),
            },
        ],
        temperature=0.1,
    )

    debug_response("Validation", resp.choices[0].message.content)
    try:
        result = _parse_json(resp.choices[0].message.content)
    except json.JSONDecodeError, KeyError:
        result = {"passed": False, "reason": "Could not parse semantic review response"}

    passed = result.get("passed", False)
    print(
        f"[Validation] {'PASSED' if passed else 'FAILED'}: {result.get('reason', '')}"
    )

    if passed:
        return {
            "validation": result,
            "patched_code": patched_code,
            "current_code": patched_code,
            "last_event": "validation_passed",
        }
    return {
        "validation": result,
        "last_event": "validation_failed",
    }
