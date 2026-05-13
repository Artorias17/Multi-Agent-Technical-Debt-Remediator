from typing import TypedDict, Annotated, Optional
import operator


class PipelineState(TypedDict):
    # Set once at startup
    report: dict  # full project JSON
    chunks: list[dict]  # file-based chunks, priority-ordered
    rule_cache: dict  # {rule_key: description}

    # Orchestrator cursor
    chunk_index: int  # which chunk is active
    attempt: int  # retry count for current chunk
    last_event: Optional[
        str
    ]  # "init" | "validation_passed" | "validation_failed" | "documentation_done" | None

    # VCS state
    vcs_mode: Optional[str]  # "finalize" | "done" | None
    repo_path: Optional[str]  # local path to cloned repo
    branch_name: Optional[str]  # feature branch name
    project_dir: Optional[
        str
    ]  # pre-cloned local repo path; if set, vcs_setup skips cloning

    # Orchestrator routing signal — set by orchestrator_node, read by route_orchestrator
    next_agent: Optional[
        str
    ]  # "context" | "remediation" | "documentation" | "finalize"

    # Working state — reset each chunk
    current_file: str
    current_issues: list[dict]
    context: Optional[dict]  # Context Agent output
    summary: Optional[dict]  # Summarizer Agent output
    diff: Optional[str]  # Remediation Agent output
    remediation_status: Optional[str]  # "passed" | "no_fix_needed" | "failed"
    remediation_reason: Optional[str]
    new_functions: list[str]  # helper names introduced by the patch
    patched_code: Optional[
        str
    ]  # set by Validation Agent on pass; consumed by Documentation Agent
    validation: Optional[dict]  # Validation Agent output
    commit_message: Optional[str]  # Documentation Agent output
    rejection_history: Annotated[list, operator.add]  # grows per retry

    # Terminal collections
    approved: Annotated[list, operator.add]
    failed: Annotated[list, operator.add]


def initialize_pipeline_state(
    report: dict, project_dir: str | None = None
) -> PipelineState:
    """Return a fully-populated initial state dict for graph.invoke()."""
    return {
        "report": report,
        "chunks": [],
        "rule_cache": {},
        "chunk_index": 0,
        "attempt": 0,
        "last_event": "init",
        "vcs_mode": None,
        "repo_path": None,
        "branch_name": None,
        "project_dir": project_dir,
        "next_agent": None,
        "current_file": "",
        "current_issues": [],
        "context": None,
        "summary": None,
        "diff": None,
        "remediation_status": None,
        "remediation_reason": None,
        "new_functions": [],
        "patched_code": None,
        "validation": None,
        "commit_message": None,
        "rejection_history": [],
        "approved": [],
        "failed": [],
    }
