import ast
import json
import time
from pathlib import Path

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
    replacement: str,
    helpers: list[dict],
    splice_start: int,
    splice_end: int,
) -> tuple[bool, str, str]:
    """
    Splice replacement + helpers into current_code at the given byte offsets.
    splice_start/splice_end come from context_agent and cover the full outermost
    statement (including any export_statement wrapper).
    """
    helper_sources = [h["source"] for h in helpers if h.get("source")]
    helpers_block = ("\n\n".join(helper_sources) + "\n\n") if helper_sources else ""
    source_bytes = current_code.encode("utf-8")
    before = source_bytes[:splice_start].decode("utf-8", errors="replace")
    after = source_bytes[splice_end:].decode("utf-8", errors="replace")
    return True, before + helpers_block + replacement + after, ""


def _apply_line_replacement(
    current_code: str, fn_start: int, fn_end: int, replacement: str
) -> tuple[bool, str, str]:
    """Replace lines fn_start..fn_end (1-based, inclusive) with replacement text."""
    lines = current_code.splitlines(keepends=True)
    start_idx = fn_start - 1
    end_idx = fn_end
    if start_idx < 0 or end_idx > len(lines):
        return False, "", f"Line range {fn_start}-{fn_end} out of bounds ({len(lines)} lines)"
    repl_lines = replacement.splitlines(keepends=True)
    if repl_lines and not repl_lines[-1].endswith("\n"):
        repl_lines[-1] += "\n"
    return True, "".join(lines[:start_idx] + repl_lines + lines[end_idx:]), ""


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

    is_snippet = fn_name == "<snippet>"
    if is_snippet:
        fn_start = fn["start"] if fn else 1
        fn_end = fn["end"] if fn else 1
        ok, patched_code, err = _apply_line_replacement(current_code, fn_start, fn_end, replacement)
        syntax_target = patched_code
    elif fn_name:
        splice_start = fn["start_byte"]
        splice_end = fn["end_byte"]
        ok, patched_code, err = _apply_replacement(current_code, replacement, helpers, splice_start, splice_end)
        syntax_target = replacement
    else:
        patched_code = replacement
        ok, err = True, ""
        syntax_target = replacement

    if not ok:
        print(f"[Validation] Replacement failed to apply: {err}")
        return {
            "validation": {"passed": False, "reason": err},
            "last_event": "validation_failed",
        }

    # ── Syntax check ─────────────────────────────────────────
    print(f"[Validation] Running syntax check ({language})")
    syntax_ok, syntax_err = _syntax_check(syntax_target, language)
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
    llm_start = time.time()
    resp = complete(client, review_messages, temperature=0.1)
    llm_elapsed = round(time.time() - llm_start, 2)

    debug_response("Validation", resp.choices[0].message.content)
    try:
        result = parse_json(resp.choices[0].message.content)
    except (json.JSONDecodeError, KeyError):
        result = {"passed": False, "reason": "Could not parse semantic review response"}

    passed = result.get("passed", False)
    print(f"[Validation] {'PASSED' if passed else 'FAILED'}: {result.get('reason', '')}")

    durations = {**(state.get("agent_durations") or {}), "validation": llm_elapsed}

    if passed:
        repo_path = state.get("repo_path")
        if repo_path:
            abs_path = Path(repo_path) / ctx["file_path"]
            abs_path.write_text(patched_code, encoding="utf-8")
            print(f"[Validation] Written to disk: {ctx['file_path']}")
        return {
            "validation": result,
            "patched_code": patched_code,
            "current_code": patched_code,
            "agent_durations": durations,
            "last_event": "validation_passed",
        }
    return {
        "validation": result,
        "agent_durations": durations,
        "last_event": "validation_failed",
    }
