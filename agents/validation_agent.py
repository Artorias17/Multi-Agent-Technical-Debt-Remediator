import ast
import json

from agents.llm import get_client, complete, debug_response, debug_request, parse_json
from state import PipelineState

_SYSTEM = (
    "You are a code review expert. "
    "Respond ONLY with valid JSON — no prose, no markdown fences."
)

_USER = """\
Review whether the patched code fully resolves the SonarQube issues listed below without introducing regressions.

Issues that must be resolved:
{issues}

Patched code:
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


def _apply_replacement(
    current_code: str,
    language: str,
    fn_name: str,
    replacement: str,
    helpers: list[dict],
) -> tuple[bool, str, str]:
    """
    Splice replacement function + helpers into current_code using tree-sitter byte offsets.
    Helpers are inserted immediately before the target function.
    Returns (success, patched_content, error_message).
    """
    from agents.context_agent import _LANGUAGE_CONFIG
    from tree_sitter import Parser

    cfg = _LANGUAGE_CONFIG.get(language)
    if cfg is None:
        return False, "", f"Unsupported language: {language}"

    source_bytes = current_code.encode("utf-8")
    parser = Parser(cfg["language"])
    tree = parser.parse(source_bytes)
    fn_node_types = cfg["function_nodes"]

    target_node = None

    def walk(node):
        nonlocal target_node
        if target_node is not None:
            return
        if node.type in fn_node_types:
            for child in node.children:
                if child.type == "identifier":
                    name = source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                    if name == fn_name:
                        target_node = node
                        return
        for child in node.children:
            walk(child)

    walk(tree.root_node)

    if target_node is None:
        return False, "", f"Function `{fn_name}` not found in source"

    helper_sources = [h["source"] for h in helpers if h.get("source")]
    helpers_block = ("\n\n".join(helper_sources) + "\n\n") if helper_sources else ""

    before = source_bytes[:target_node.start_byte].decode("utf-8", errors="replace")
    after = source_bytes[target_node.end_byte:].decode("utf-8", errors="replace")
    patched = before + helpers_block + replacement + after

    return True, patched, ""


def _syntax_check(patched_code: str, language: str) -> tuple[bool, str]:
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
        return True, ""

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


def validation_node(state: PipelineState) -> dict:
    remediation_status = state.get("remediation_status", "passed")
    ctx = state["context"]
    language = ctx.get("language", "unknown")

    if remediation_status == "no_fix_needed":
        return {
            "validation": {
                "passed": True,
                "reason": state.get("remediation_reason", "no fix required"),
            },
            "patched_code": ctx["full_code"],
            "last_event": "validation_passed",
        }

    replacement = state.get("replacement")
    if not replacement:
        return {
            "validation": {
                "passed": False,
                "reason": "No replacement was produced by the Remediation Agent",
            },
            "last_event": "validation_failed",
        }

    issues = state["current_issues"]
    helpers = state.get("helpers") or []
    current_code = state.get("current_code") or ctx["full_code"]

    functions = ctx.get("functions", [])
    fn = functions[0] if functions else None
    fn_name = fn["name"] if fn else None

    print(f"[Validation] Applying replacement to {ctx['file_path']}")

    if fn_name:
        ok, patched_code, err = _apply_replacement(current_code, language, fn_name, replacement, helpers)
    else:
        patched_code = replacement
        ok, err = True, ""

    if not ok:
        print(f"[Validation] Replacement failed to apply: {err}")
        return {
            "validation": {"passed": False, "reason": err},
            "last_event": "validation_failed",
        }

    # ── Syntax check on replacement function ────────────────
    print(f"[Validation] Running syntax check ({language})")
    syntax_ok, syntax_err = _syntax_check(replacement, language)
    if not syntax_ok:
        print(f"[Validation] Syntax check FAILED: {syntax_err}")
        return {
            "validation": {"passed": False, "reason": f"Syntax error: {syntax_err}"},
            "last_event": "validation_failed",
        }

    # ── Semantic LLM review on replacement + helpers ─────────
    print("[Validation] Running semantic review")
    helper_sources = [h["source"] for h in helpers if h.get("source")]
    review_code = replacement
    if helper_sources:
        review_code = "\n\n".join(helper_sources) + "\n\n" + replacement

    client = get_client()
    review_messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _USER.format(
            issues=_format_issues(issues),
            patched_code=review_code,
        )},
    ]
    debug_request("Validation", review_messages)
    resp = complete(client, review_messages, temperature=0.1)

    debug_response("Validation", resp.choices[0].message.content)
    try:
        result = parse_json(resp.choices[0].message.content)
    except (json.JSONDecodeError, KeyError):
        result = {"passed": False, "reason": "Could not parse semantic review response"}

    passed = result.get("passed", False)
    print(f"[Validation] {'PASSED' if passed else 'FAILED'}: {result.get('reason', '')}")

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
