import json
import re

from agents.llm import get_client, MODEL, debug_response
from state import PipelineState

# ── Signature patterns to detect new helpers in a diff ───────

_SIG_PATTERNS: dict[str, re.Pattern] = {
    "java":       re.compile(r"^\+\s*(?:private|protected|public|static)\s+\S+\s+(\w+)\s*\("),
    "csharp":     re.compile(r"^\+\s*(?:private|protected|public|static)\s+\S+\s+(\w+)\s*\("),
    "javascript": re.compile(r"^\+\s*(?:function\s+(\w+)|(?:const|let)\s+(\w+)\s*=\s*(?:\(.*\)|[^=]+)\s*=>)"),
    "typescript": re.compile(r"^\+\s*(?:function\s+(\w+)|(?:const|let)\s+(\w+)\s*=\s*(?:\(.*\)|[^=]+)\s*=>)"),
    "python":     re.compile(r"^\+\s*def\s+(\w+)\s*\("),
}

_TRIAGE_SYSTEM = (
    "You are a {language} refactoring expert. "
    "Respond ONLY with valid JSON — no prose, no markdown fences."
)

_TRIAGE_USER = """\
The following SonarQube issues were found in a {language} function.
Decide whether a code change is required to resolve them.

Issues:
{issues}

Function source:
{source}

Return a JSON object:
{{"fix_needed": true|false, "reason": "<brief explanation>"}}
"""

_PATCH_SYSTEM = (
    "You are a {language} refactoring expert. "
    "Output ONLY a unified diff in standard diff -u format inside a ```diff fence. "
    "No prose before or after. "
    "If you extract helpers, give them descriptive names."
)

_PATCH_USER = """\
Fix the SonarQube issues listed below without changing the observable behaviour described in the summary.

File: {file_path}

Issues:
{issues}

Behavioural contract (DO NOT break this):
{summary}
{rejection_block}
Existing imports (do NOT add any import/using/require not already present):
{import_block}

Existing names (do NOT reuse any of these names for new helper functions):
{name_inventory}

Constraints:
- Do NOT change any public method or function signature.
- This function starts at line {fn_start} in the file — use full-file line numbers in the diff.

Current function:
```
{fn_source}
```
"""

# ── Helpers ──────────────────────────────────────────────────

def _format_issues(issues: list[dict]) -> str:
    lines = []
    for i in issues:
        lines.append(
            f"- [{i.get('severity', '?')}] {i.get('rule', '?')}: "
            f"{i.get('action_message', '')} (line {i.get('start_line', '?')})"
        )
    return "\n".join(lines)


def _format_summary(summary: list[dict]) -> str:
    if not summary:
        return "(no summary available)"
    parts = []
    for s in summary:
        params = ", ".join(
            f"{p['name']}: {p.get('type', '?')} — {p.get('purpose', '')}"
            for p in s.get("parameters", [])
        )
        parts.append(
            f"Function `{s['function_name']}`: {s['description']}\n"
            f"  Params: {params or 'none'}\n"
            f"  Returns: {s.get('returns', 'unknown')}"
        )
    return "\n".join(parts)


def _format_rejection_block(rejection_history: list[dict]) -> str:
    if not rejection_history:
        return ""
    lines = ["\nPrevious attempts failed — avoid repeating these mistakes:"]
    for r in rejection_history:
        lines.append(f"  Attempt {r.get('attempt', '?')}: {r.get('reason', '')}")
    return "\n".join(lines) + "\n"


def _extract_diff(text: str) -> str | None:
    """Pull the diff from a ```diff ... ``` block, or return None."""
    match = re.search(r"```diff\s*(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Fallback: if the response starts with --- it may be a raw diff
    if re.search(r"^---\s", text, re.MULTILINE):
        return text.strip()
    return None


def _extract_new_functions(diff: str, language: str) -> list[str]:
    pattern = _SIG_PATTERNS.get(language)
    if not pattern:
        return []
    names: list[str] = []
    for line in diff.splitlines():
        m = pattern.match(line)
        if m:
            # Some patterns have multiple capture groups
            name = next((g for g in m.groups() if g), None)
            if name and name not in names:
                names.append(name)
    return names


def _parse_json(text: str) -> dict:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


# ── Node ─────────────────────────────────────────────────────

def remediation_node(state: PipelineState) -> dict:
    ctx = state["context"]
    language = ctx.get("language", "unknown")
    functions = ctx.get("functions", [])
    issues = state["current_issues"]
    summary = state.get("summary") or []
    rejection_history = state.get("rejection_history") or []

    fn = functions[0] if functions else None
    fn_source = fn["source"] if fn else ctx.get("full_code", "")
    fn_start = fn["start"] if fn else 1

    if fn:
        fn_issues = [i for i in issues if fn["start"] <= (i.get("start_line") or 0) <= fn["end"]]
        issues = fn_issues or issues

    issue_text = _format_issues(issues)
    import_block = ctx.get("import_block", "(none)") or "(none)"
    name_inventory = "\n".join(ctx.get("name_inventory", [])) or "(none)"

    client = get_client()

    # ── Step 1: triage ───────────────────────────────────────
    print(f"[Remediation] Triaging {len(issues)} issue(s) in {ctx['file_path']}")
    triage_resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _TRIAGE_SYSTEM.format(language=language)},
            {"role": "user", "content": _TRIAGE_USER.format(
                language=language,
                issues=issue_text,
                source=fn_source,
            )},
        ],
        temperature=0.1,
    )

    debug_response("Remediation/triage", triage_resp.choices[0].message.content)
    try:
        triage = _parse_json(triage_resp.choices[0].message.content)
    except (json.JSONDecodeError, KeyError):
        triage = {"fix_needed": True, "reason": "triage parse failed, attempting fix"}

    if not triage.get("fix_needed", True):
        print(f"[Remediation] No fix needed: {triage.get('reason')}")
        return {
            "diff": None,
            "remediation_status": "no_fix_needed",
            "remediation_reason": triage.get("reason", ""),
            "new_functions": [],
        }

    # ── Step 2: generate patch ───────────────────────────────
    print("[Remediation] Generating patch")
    patch_resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": _PATCH_SYSTEM.format(language=language)},
            {"role": "user", "content": _PATCH_USER.format(
                language=language,
                file_path=ctx["file_path"],
                issues=issue_text,
                summary=_format_summary(summary),
                rejection_block=_format_rejection_block(rejection_history),
                import_block=import_block,
                name_inventory=name_inventory,
                fn_source=fn_source,
                fn_start=fn_start,
            )},
        ],
        temperature=0.2,
    )

    raw = patch_resp.choices[0].message.content
    debug_response("Remediation/patch", raw)
    diff = _extract_diff(raw)

    if not diff:
        print("[Remediation] Could not extract diff from LLM response")
        return {
            "diff": None,
            "remediation_status": "failed",
            "remediation_reason": "LLM response did not contain a parseable unified diff",
            "new_functions": [],
        }

    new_functions = _extract_new_functions(diff, language)
    if new_functions:
        print(f"[Remediation] New helpers detected: {new_functions}")

    return {
        "diff": diff,
        "remediation_status": "passed",
        "remediation_reason": triage.get("reason", ""),
        "new_functions": new_functions,
    }
