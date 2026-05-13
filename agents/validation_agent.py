import ast
import json
import subprocess
import tempfile
from pathlib import Path

from agents.llm import get_client, MODEL
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


def _apply_diff_to_temp(full_code: str, diff: str) -> tuple[bool, str, str]:
    """
    Apply diff to a temp copy of the file.
    Returns (success, patched_content, error_message).
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        target = Path(tmp_dir) / "file.tmp"
        target.write_text(full_code, encoding="utf-8")

        result = subprocess.run(
            ["patch", "-u", str(target)],
            input=diff.encode(),
            capture_output=True,
        )
        if result.returncode != 0:
            return False, "", result.stderr.decode()

        return True, target.read_text(encoding="utf-8"), ""


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

    print(f"[Validation] Applying patch to temp copy of {ctx['file_path']}")

    # ── Phase 1: structural check (patch applies cleanly) ────
    ok, patched_code, err = _apply_diff_to_temp(ctx["full_code"], diff)
    if not ok:
        print(f"[Validation] Patch failed to apply: {err.strip()}")
        return {
            "validation": {
                "passed": False,
                "reason": f"Patch did not apply cleanly: {err.strip()}",
            },
            "last_event": "validation_failed",
        }

    # ── Phase 0: syntax check ─────────────────────────────────
    print(f"[Validation] Running syntax check ({language})")
    syntax_ok, syntax_err = _syntax_check(patched_code, language)
    if not syntax_ok:
        print(f"[Validation] Syntax check FAILED: {syntax_err}")
        return {
            "validation": {
                "passed": False,
                "reason": f"Syntax error in patched file: {syntax_err}",
            },
            "last_event": "validation_failed",
        }

    # ── Phase 2: semantic LLM review ─────────────────────────
    print("[Validation] Running semantic review")
    client = get_client()
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _USER.format(
                issues=_format_issues(issues),
                patched_code=patched_code,
            )},
        ],
        temperature=0.1,
    )

    try:
        result = _parse_json(resp.choices[0].message.content)
    except (json.JSONDecodeError, KeyError):
        result = {"passed": False, "reason": "Could not parse semantic review response"}

    passed = result.get("passed", False)
    print(f"[Validation] {'PASSED' if passed else 'FAILED'}: {result.get('reason', '')}")

    if passed:
        return {
            "validation": result,
            "patched_code": patched_code,
            "last_event": "validation_passed",
        }
    return {
        "validation": result,
        "last_event": "validation_failed",
    }
