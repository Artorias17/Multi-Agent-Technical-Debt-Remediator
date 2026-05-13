from pathlib import Path

from state import PipelineState

MAX_RETRIES = 3

SEVERITY_WEIGHT = {
    "BLOCKER": 5,
    "CRITICAL": 4,
    "MAJOR": 3,
    "MINOR": 2,
    "INFO": 1,
}

# ── Chunk building ───────────────────────────────────────────

# TODO: Update priority formula once the new dataset provides per-file
# cognitive_complexity and line_coverage. Current formula uses only
# fields present in the issue list (severity, effort_minutes, count).
def _build_chunks(report: dict) -> list[dict]:
    """
    Group issues by file (component), compute a priority score per chunk,
    and return chunks sorted highest priority first.

    Priority score = 0.5 * norm(max_severity)
                   + 0.3 * norm(sum_effort)
                   + 0.2 * norm(issue_count)
    """
    issues = report.get("issues", [])

    # Group by file
    file_map: dict[str, list[dict]] = {}
    for issue in issues:
        comp = issue.get("component") or "unknown"
        file_map.setdefault(comp, []).append(issue)

    raw_chunks = []
    for file_path, file_issues in file_map.items():
        max_sev = max(
            SEVERITY_WEIGHT.get(i.get("severity", "INFO"), 1) for i in file_issues
        )
        sum_effort = sum(i.get("effort_minutes") or 0 for i in file_issues)
        count = len(file_issues)
        raw_chunks.append(
            {
                "file": file_path,
                "issues": file_issues,
                "_max_sev": max_sev,
                "_sum_effort": sum_effort,
                "_count": count,
            }
        )

    if not raw_chunks:
        return []

    # Normalize each dimension to [0, 1] then compute weighted score
    max_sev_val = max(c["_max_sev"] for c in raw_chunks) or 1
    max_effort_val = max(c["_sum_effort"] for c in raw_chunks) or 1
    max_count_val = max(c["_count"] for c in raw_chunks) or 1

    for chunk in raw_chunks:
        chunk["priority_score"] = round(
            0.5 * (chunk["_max_sev"] / max_sev_val)
            + 0.3 * (chunk["_sum_effort"] / max_effort_val)
            + 0.2 * (chunk["_count"] / max_count_val),
            4,
        )
        del chunk["_max_sev"]
        del chunk["_sum_effort"]
        del chunk["_count"]

    return sorted(raw_chunks, key=lambda c: c["priority_score"], reverse=True)


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
    """Clear per-chunk working state before dispatching a new chunk."""
    return {
        "attempt": 0,
        "current_file": "",
        "current_issues": [],
        "context": None,
        "summary": None,
        "diff": None,
        "patched_code": None,
        "validation": None,
        "rejection_history": [],
    }


# ── Routing helper ───────────────────────────────────────────


def _next_chunk_or_finalize(state: dict, current_index: int, chunks: list) -> dict:
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
        "chunk_index": next_index,
        "current_file": next_chunk["file"],
        "current_issues": next_chunk["issues"],
        "next_agent": "context",
        **_reset_chunk_state(),
    }


# ── Conditional edge (imported by graph.py) ──────────────────


def route_orchestrator(state: dict) -> str:
    return {
        "vcs_setup":     "vcs_setup",
        "context":       "context_agent",
        "remediation":   "remediation_agent",
        "documentation": "documentation_agent",
        "finalize":      "vcs_finalize",
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
            "chunks": chunks,
            "chunk_index": 0,
            "rule_cache": rule_cache,
            "current_file": first_chunk["file"],
            "current_issues": first_chunk["issues"],
            "next_agent": "context",
            "last_event": None,
            **_reset_chunk_state(),
        }

    chunks = state["chunks"]
    chunk_index = state["chunk_index"]
    current = chunks[chunk_index]

    # ── Validation passed → send to documentation ────────────
    if event == "validation_passed":
        attempt = state.get("attempt", 0)
        print(
            f"[Orchestrator] Validation PASSED on attempt {attempt} "
            f"({Path(current['file']).name}) — routing to documentation"
        )
        return {"next_agent": "documentation"}

    # ── Validation failed → retry or exhaust ─────────────────
    if event == "validation_failed":
        attempt = state.get("attempt", 0)
        new_attempt = attempt + 1
        validation = state.get("validation") or {}
        rejection_entry = {
            "attempt": attempt,
            "reason": validation.get("reason", "unknown"),
        }

        if new_attempt < MAX_RETRIES:
            print(
                f"[Orchestrator] Validation FAILED (attempt {attempt}) — "
                f"retry {new_attempt} ({Path(current['file']).name})"
            )
            return {
                "attempt": new_attempt,
                "rejection_history": [rejection_entry],
                "next_agent": "remediation",
            }

        # Retries exhausted → mark failed, advance
        print(
            f"[Orchestrator] Validation FAILED — retries exhausted "
            f"({Path(current['file']).name}), marking chunk failed"
        )
        failed_entry = {
            "task": {
                "rule_key": [i.get("rule") for i in current["issues"]],
                "component": current["file"],
            },
            "rejection_history": state.get("rejection_history", []) + [rejection_entry],
        }
        return {
            **_next_chunk_or_finalize(state, chunk_index, chunks),
            "failed": [failed_entry],
            "last_event": None,
        }

    # ── Documentation done → advance to next chunk ───────────
    if event == "documentation_done":
        print(
            f"[Orchestrator] Documentation DONE for chunk {chunk_index} "
            f"({Path(current['file']).name}) — advancing"
        )
        return {
            **_next_chunk_or_finalize(state, chunk_index, chunks),
            "last_event": None,
        }

    # Fallback (shouldn't normally be reached)
    return {
        **_next_chunk_or_finalize(state, chunk_index, chunks),
        "last_event": None,
    }
