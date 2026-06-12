import json
import time

from agents.llm import get_client, complete, debug_response, debug_request, parse_json
from state import PipelineState

_TRIAGE_SYSTEM = (
    "You are a {language} refactoring expert. "
    "Respond ONLY with valid JSON — no prose, no markdown fences."
)

_TRIAGE_USER = """\
The following SonarQube issues were found in a {language} function.
Decide whether a code change is required to resolve them.

Issues:
{issues}

Code:
{source}

Return a JSON object:
{{"fix_needed": true|false, "reason": "<brief explanation>"}}
"""

_PATCH_SYSTEM = (
    "You are a {language} refactoring expert. "
    "Respond ONLY with valid JSON — no prose, no markdown fences."
)

_PATCH_USER = """\
Fix the SonarQube issues listed below without changing the observable behaviour described in the summary.

File: {file_path}
Function: `{fn_name}` (starts at line {fn_start})

Issues:
{issues}

Behavioural contract (DO NOT break this):
{summary}
{rejection_block}
Existing imports (do NOT add any import/using/require not already present):
{import_block}

Existing function signatures (do NOT reuse these names for new helpers):
{name_inventory}

Constraints:
- Do NOT change any public method or function signature.
- Return the complete, fixed source of `{fn_name}` only — no imports, no class wrapper, just the function.
- If you extract helper functions, return their complete source in the helpers array.
- Before returning, audit every helper: every variable it uses must be declared locally, passed as an explicit parameter, or be a module-level import or global. Do NOT implicitly capture variables from the calling function's scope — pass them explicitly as parameters.

Current function:
```
{fn_source}
```

Return a JSON object with this exact shape:
{{
  "replacement": "<complete fixed source of `{fn_name}`>",
  "helpers": [
    {{"name": "<helper name>", "source": "<complete helper source>"}}
  ]
}}
If no helpers are needed, return an empty array for helpers.
"""

_SNIPPET_PATCH_USER = """\
Fix the SonarQube issues listed below in the module-level code shown.

File: {file_path}
Lines: {fn_start}–{fn_end}

Issues:
{issues}
{rejection_block}
Existing imports (do NOT add any import/using/require not already present):
{import_block}

Constraints:
- Do NOT wrap the code in a function or class.
- Return only the fixed version of exactly the lines shown.
- Do NOT add new imports.

Current code (lines {fn_start}–{fn_end}):
```
{fn_source}
```

Return a JSON object:
{{"replacement": "<fixed version of the shown lines>"}}
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
    fn_end = fn["end"] if fn else 1
    fn_name = fn["name"] if fn else "<unknown>"

    if fn:
        fn_issues = [i for i in issues if fn["start"] <= (i.get("start_line") or 0) <= fn["end"]]
        issues = fn_issues or issues

    issue_text = _format_issues(issues)
    import_block = ctx.get("import_block", "(none)") or "(none)"
    inv = ctx.get("name_inventory") or {}
    if isinstance(inv, dict):
        name_inventory = "\n".join(f"{n}: {s}" for n, s in inv.items()) or "(none)"
    else:
        name_inventory = "\n".join(inv) or "(none)"

    client = get_client()
    node_start = time.time()

    # ── Step 1: triage ───────────────────────────────────────
    print(f"[Remediation] Triaging {len(issues)} issue(s) in {ctx['file_path']}")
    triage_messages = [
        {"role": "system", "content": _TRIAGE_SYSTEM.format(language=language)},
        {"role": "user", "content": _TRIAGE_USER.format(
            language=language,
            issues=issue_text,
            source=fn_source,
        )},
    ]
    debug_request("Remediation/triage", triage_messages)
    triage_resp = complete(client, triage_messages, temperature=0.1)

    debug_response("Remediation/triage", triage_resp.choices[0].message.content)
    try:
        triage = parse_json(triage_resp.choices[0].message.content)
    except (json.JSONDecodeError, KeyError):
        triage = {"fix_needed": True, "reason": "triage parse failed, attempting fix"}

    if not triage.get("fix_needed", True):
        print(f"[Remediation] No fix needed: {triage.get('reason')}")
        elapsed = round(time.time() - node_start, 2)
        return {
            "replacement": None,
            "helpers": [],
            "remediation_status": "no_fix_needed",
            "remediation_reason": triage.get("reason", ""),
            "new_functions": [],
            "agent_durations": {**(state.get("agent_durations") or {}), "remediation": elapsed},
        }

    # ── Step 2: generate replacement ────────────────────────
    print("[Remediation] Generating replacement")
    is_snippet = fn_name == "<snippet>"
    if is_snippet:
        patch_user = _SNIPPET_PATCH_USER.format(
            file_path=ctx["file_path"],
            fn_start=fn_start,
            fn_end=fn_end,
            issues=issue_text,
            rejection_block=_format_rejection_block(rejection_history),
            import_block=import_block,
            fn_source=fn_source,
        )
    else:
        patch_user = _PATCH_USER.format(
            language=language,
            file_path=ctx["file_path"],
            issues=issue_text,
            summary=_format_summary(summary),
            rejection_block=_format_rejection_block(rejection_history),
            import_block=import_block,
            name_inventory=name_inventory,
            fn_source=fn_source,
            fn_start=fn_start,
            fn_name=fn_name,
        )
    patch_messages = [
        {"role": "system", "content": _PATCH_SYSTEM.format(language=language)},
        {"role": "user", "content": patch_user},
    ]
    debug_request("Remediation/patch", patch_messages)
    patch_resp = complete(client, patch_messages, temperature=0.2)

    raw = patch_resp.choices[0].message.content
    debug_response("Remediation/patch", raw)

    try:
        result = parse_json(raw)
        replacement = result.get("replacement", "").strip()
        helpers = result.get("helpers", [])
    except (json.JSONDecodeError, KeyError):
        print("[Remediation] Could not parse replacement from LLM response")
        elapsed = round(time.time() - node_start, 2)
        return {
            "replacement": None,
            "helpers": [],
            "remediation_status": "failed",
            "remediation_reason": "LLM response did not contain parseable replacement JSON",
            "new_functions": [],
            "agent_durations": {**(state.get("agent_durations") or {}), "remediation": elapsed},
        }

    if not replacement:
        print("[Remediation] Empty replacement in LLM response")
        elapsed = round(time.time() - node_start, 2)
        return {
            "replacement": None,
            "helpers": [],
            "remediation_status": "failed",
            "remediation_reason": "LLM returned empty replacement function",
            "new_functions": [],
            "agent_durations": {**(state.get("agent_durations") or {}), "remediation": elapsed},
        }

    if is_snippet:
        helpers = []
    new_functions = [h["name"] for h in helpers if h.get("name")]
    if new_functions:
        print(f"[Remediation] New helpers: {new_functions}")

    elapsed = round(time.time() - node_start, 2)
    return {
        "replacement": replacement,
        "helpers": helpers,
        "remediation_status": "passed",
        "remediation_reason": triage.get("reason", ""),
        "new_functions": new_functions,
        "agent_durations": {**(state.get("agent_durations") or {}), "remediation": elapsed},
    }
