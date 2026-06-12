import time
from pathlib import Path
from state import PipelineState
from checkpoint import write_issues
from agents.context_agent import signature_from_source

MAX_RETRIES = 3

SEVERITY_WEIGHT = {
    "BLOCKER": 5,
    "CRITICAL": 4,
    "MAJOR": 3,
    "MINOR": 2,
    "INFO": 1,
}

# ── Chunk building ───────────────────────────────────────────


def _build_chunks(report: dict) -> list[dict]:
    """
    Group issues by file (component) and sort by max severity descending,
    breaking ties by issue count descending.
    """
    issues = report.get("issues", [])

    file_map: dict[str, list[dict]] = {}
    for issue in issues:
        comp = issue.get("component") or "unknown"
        file_map.setdefault(comp, []).append(issue)

    chunks = []
    for file_path, file_issues in file_map.items():
        max_sev = max(
            SEVERITY_WEIGHT.get(i.get("severity", "INFO"), 1) for i in file_issues
        )
        chunks.append(
            {
                "file": file_path,
                "issues": file_issues,
                "priority_score": max_sev,
            }
        )

    return sorted(
        chunks, key=lambda c: (c["priority_score"], len(c["issues"])), reverse=True
    )


def _build_rule_cache(report: dict) -> dict[str, str | None]:
    """Build {rule_key: description} from all issues."""
    cache = {}
    for issue in report.get("issues", []):
        rule = issue.get("rule")
        if rule and rule not in cache:
            cache[rule] = issue.get("description")
    return cache


# ── State resets ─────────────────────────────────────────────


def _reset_chunk_state() -> dict:
    """Clear all per-chunk and per-function state when dispatching a new chunk."""
    return {
        "attempt": 0,
        "current_file": "",
        "current_issues": [],
        "context": None,
        "summary": None,
        "replacement": None,
        "helpers": [],
        "patched_code": None,
        "validation": None,
        "rejection_history": [],
        "function_index": 0,
        "functions_to_process": [],
        "current_code": "",
        "remediation_status": None,
        "remediation_reason": None,
        "new_functions": [],
        "agent_durations": {},
        "function_start_time": None,
    }


def _reset_function_state() -> dict:
    """Clear per-function attempt state when advancing to the next function."""
    return {
        "attempt": 0,
        "summary": None,
        "replacement": None,
        "helpers": [],
        "patched_code": None,
        "validation": None,
        "rejection_history": [],
        "remediation_status": None,
        "remediation_reason": None,
        "new_functions": [],
        "agent_durations": {},
        "function_start_time": None,
    }


# ── Routing helpers ───────────────────────────────────────────


def _next_chunk_or_finalize(
    state: PipelineState, current_index: int, chunks: list
) -> dict:
    next_index = current_index + 1

    if next_index >= len(chunks):
        print("[Orchestrator] All chunks processed → finalizing VCS")
        return {"chunk_index": next_index, "next_agent": "finalize"}

    next_chunk = chunks[next_index]
    print(
        f"[Orchestrator] Dispatching chunk {next_index}: "
        f"{Path(next_chunk['file']).name} "
        f"({len(next_chunk['issues'])} issues, "
        f"score={next_chunk['priority_score']})"
    )

    return {
        **_reset_chunk_state(),
        "chunk_index": next_index,
        "current_file": next_chunk["file"],
        "current_issues": next_chunk["issues"],
        "next_agent": "context",
    }


def _advance_function(state: PipelineState) -> dict:
    """
    Move to the next function in the current chunk.
    If all functions are done, advance to the next chunk (or finalize).
    """
    functions_to_process = state.get("functions_to_process", [])
    next_index = state.get("function_index", 0) + 1

    if next_index >= len(functions_to_process):
        print("[Orchestrator] All functions processed — committing chunk")
        return {**_reset_function_state(), "next_agent": "commit_chunk"}

    next_entry = functions_to_process[next_index]
    fn = next_entry["fn"]
    issues = next_entry["issues"]
    current_context = state.get("context") or {}

    print(
        f"[Orchestrator] Advancing to function {next_index}: "
        f"`{fn['name']}` ({len(issues)} issue(s))"
    )

    return {
        **_reset_function_state(),
        "function_index": next_index,
        "current_issues": issues,
        "context": {**current_context, "functions": [fn]},
        "function_start_time": time.time(),
        "next_agent": "summarizer",
    }


# ── Checkpoint writer ─────────────────────────────────────────


def _write_checkpoint_entries(state: PipelineState, resolved: bool, extra_rejection: dict | None = None) -> None:
    path = state.get("checkpoint_path")
    if not path:
        return
    issues = state.get("current_issues") or []
    ctx = state.get("context") or {}
    fn_name = ((ctx.get("functions") or [{}])[0]).get("name", "?")
    agent_durations = state.get("agent_durations") or {}
    rejection_history = list(state.get("rejection_history") or [])
    if extra_rejection:
        rejection_history.append(extra_rejection)
    start_time = state.get("function_start_time")
    total = round(time.time() - start_time, 2) if start_time else None

    rejection_reasons = [
        f"Attempt {r.get('attempt', '?')}: {r.get('reason', '')}"
        for r in rejection_history
    ]
    entries = [
        {
            "id": issue.get("id", issue.get("key", "?")),
            "rule": issue.get("rule", "?"),
            "severity": issue.get("severity", "?"),
            "file": ctx.get("file_path", "?"),
            "function": fn_name,
            "resolved": resolved,
            "rejection_reasons": rejection_reasons,
            "agent_durations": agent_durations,
            "total_duration_seconds": total,
        }
        for issue in issues
    ]
    write_issues(Path(path), entries)


# ── Conditional edge (imported by graph.py) ──────────────────


def route_orchestrator(state: dict) -> str:
    return {
        "vcs_setup": "vcs_setup",
        "context": "context_agent",
        "summarizer": "summarizer_agent",
        "remediation": "remediation_agent",
        "documentation": "documentation_agent",
        "commit_chunk": "vcs_commit_chunk",
        "finalize": "vcs_finalize",
    }.get(state.get("next_agent", "vcs_setup"), "vcs_setup")


# ── Main orchestrator node ────────────────────────────────────


def orchestrator_node(state: PipelineState) -> dict:
    event = state.get("last_event")

    # ── Phase 1: first call → trigger VCS setup ─────────────
    if event == "init":
        print("[Orchestrator] Init — dispatching VCS setup")
        return {"next_agent": "vcs_setup"}

    # ── Phase 2: VCS ready → build chunks, dispatch first ───
    if event == "vcs_ready":
        report = state["report"]
        chunks = _build_chunks(report)
        rule_cache = _build_rule_cache(report)

        print(
            f"[Orchestrator] {len(chunks)} file chunks, {len(rule_cache)} unique rules"
        )

        if not chunks:
            print("[Orchestrator] No open issues found — going straight to finalize.")
            return {
                "chunks": [],
                "chunk_index": 0,
                "rule_cache": rule_cache,
                "next_agent": "finalize",
            }

        first_chunk = chunks[0]
        print(
            f"[Orchestrator] Dispatching chunk 0: "
            f"{Path(first_chunk['file']).name} "
            f"({len(first_chunk['issues'])} issues, "
            f"score={first_chunk['priority_score']})"
        )

        return {
            **_reset_chunk_state(),
            "chunks": chunks,
            "chunk_index": 0,
            "rule_cache": rule_cache,
            "current_file": first_chunk["file"],
            "current_issues": first_chunk["issues"],
            "next_agent": "context",
            "last_event": None,
        }

    chunks = state["chunks"]
    chunk_index = state["chunk_index"]
    current = chunks[chunk_index]

    # ── Context ready → start function loop ──────────────────
    if event == "context_ready":
        functions_to_process = state.get("functions_to_process", [])
        file_name = Path(current["file"]).name

        # Skip functions whose issues are all already in the checkpoint
        checkpointed_ids = set(state.get("checkpointed_issue_ids") or [])
        if checkpointed_ids:
            before = len(functions_to_process)
            functions_to_process = [
                entry for entry in functions_to_process
                if not all(i.get("id", "") in checkpointed_ids for i in entry["issues"])
            ]
            skipped = before - len(functions_to_process)
            if skipped:
                print(f"[Orchestrator] Skipping {skipped} already-checkpointed function(s) in {file_name}")

        if not functions_to_process:
            print(
                f"[Orchestrator] No processable functions in {file_name} — skipping chunk"
            )
            return {
                "functions_to_process": [],
                **_next_chunk_or_finalize(state, chunk_index, chunks),
                "last_event": None,
            }

        first = functions_to_process[0]
        print(
            f"[Orchestrator] Starting function 0: "
            f"`{first['fn']['name']}` ({len(first['issues'])} issue(s)) in {file_name}"
        )
        current_context = state.get("context") or {}
        return {
            "functions_to_process": functions_to_process,
            "current_issues": first["issues"],
            "context": {**current_context, "functions": [first["fn"]]},
            "function_start_time": time.time(),
            "next_agent": "summarizer",
            "last_event": None,
        }

    # ── Validation passed → documentation (or skip if no fix) ─
    if event == "validation_passed":
        attempt = state.get("attempt", 0)
        fn_name = ((state.get("context") or {}).get("functions") or [{}])[0].get(
            "name", "?"
        )
        print(
            f"[Orchestrator] Validation PASSED on attempt {attempt} "
            f"(`{fn_name}`) — advancing"
        )
        if state.get("remediation_status") == "no_fix_needed":
            _write_checkpoint_entries(state, resolved=False)
            return {**_advance_function(state), "last_event": None}
        return {"next_agent": "documentation", "last_event": None}

    # ── Validation failed → retry or skip function ───────────
    if event == "validation_failed":
        attempt = state.get("attempt", 0)
        new_attempt = attempt + 1
        validation = state.get("validation") or {}
        fn_name = ((state.get("context") or {}).get("functions") or [{}])[0].get(
            "name", "?"
        )
        rejection_entry = {
            "attempt": attempt,
            "reason": validation.get("reason", "unknown"),
        }

        if new_attempt < MAX_RETRIES:
            print(
                f"[Orchestrator] Validation FAILED (attempt {attempt}) — "
                f"retry {new_attempt} (`{fn_name}`)"
            )
            return {
                "attempt": new_attempt,
                "rejection_history": state.get("rejection_history", [])
                + [rejection_entry],
                "next_agent": "remediation",
            }

        print(
            f"[Orchestrator] Validation FAILED (attempt {attempt}) — retries exhausted "
            f"(`{fn_name}`), skipping function"
        )
        _write_checkpoint_entries(state, resolved=False, extra_rejection=rejection_entry)
        failed_entry = {
            "task": {
                "rule_key": [i.get("rule") for i in state.get("current_issues", [])],
                "component": current["file"],
                "function": fn_name,
                "issues": state.get("current_issues", []),
            },
            "rejection_history": state.get("rejection_history", []) + [rejection_entry],
        }
        return {
            **_advance_function(state),
            "failed": [failed_entry],
            "last_event": None,
        }

    # ── Chunk committed → advance to next chunk ──────────────
    if event == "chunk_committed":
        print(f"[Orchestrator] Chunk committed — advancing")
        return {
            **_next_chunk_or_finalize(state, chunk_index, chunks),
            "last_event": None,
        }

    # ── Documentation done → advance to next function (or chunk)
    if event == "documentation_done":
        ctx = state.get("context") or {}
        fn = (ctx.get("functions") or [{}])[0]
        fn_name = fn.get("name", "?")
        print(f"[Orchestrator] Documentation DONE (`{fn_name}`) — advancing")
        _write_checkpoint_entries(state, resolved=True)

        # Update signature inventory so later functions know about new/changed helpers
        inv = dict(ctx.get("name_inventory") or {})
        language = ctx.get("language", "")
        replacement = state.get("replacement") or ""
        if replacement and fn_name not in ("<anonymous>", "<snippet>", "?"):
            inv[fn_name] = signature_from_source(fn_name, replacement, language)
        for helper in (state.get("helpers") or []):
            h_name = helper.get("name", "")
            h_src = helper.get("source", "")
            if h_name and h_src:
                inv[h_name] = signature_from_source(h_name, h_src, language)

        advanced = _advance_function(state)
        if inv and "context" in advanced:
            advanced = {**advanced, "context": {**advanced["context"], "name_inventory": inv}}
        return {**advanced, "last_event": None}

    # Fallback
    return {**_next_chunk_or_finalize(state, chunk_index, chunks), "last_event": None}
