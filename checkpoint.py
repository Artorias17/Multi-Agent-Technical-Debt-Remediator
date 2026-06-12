import json
from datetime import datetime, timezone
from pathlib import Path


def get_project_key(report: dict) -> str:
    issues = report.get("issues", [])
    if issues:
        component = issues[0].get("component", "")
        key = component.split(":")[0]
        return key or "unknown"
    return "unknown"


def checkpoint_path(project_key: str) -> Path:
    p = Path("checkpoint")
    p.mkdir(exist_ok=True)
    return p / f"checkpoint_{project_key}.jsonl"


def write_header(path: Path, branch: str, started_at: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "header", "branch": branch, "started_at": started_at}) + "\n")


def write_issues(path: Path, entries: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps({"type": "issue", **entry}) + "\n")


def write_footer(path: Path, finished_at: str, pr_url: str | None) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "footer", "finished_at": finished_at, "pr_url": pr_url}) + "\n")


def write_resume_marker(path: Path, resumed_at: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "resume", "resumed_at": resumed_at}) + "\n")


def read_checkpoint_stats(path: Path) -> dict:
    """Return {"resolved": list[dict], "failed": list[dict]} from issue lines in checkpoint.
    Deduplicates by issue ID — last write wins — to handle partial-write-then-resume."""
    seen: dict[str, dict] = {}
    if not path.exists():
        return {"resolved": [], "failed": []}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "issue":
                continue
            seen[rec.get("id", "")] = rec
    resolved = [r for r in seen.values() if r.get("resolved")]
    failed = [r for r in seen.values() if not r.get("resolved")]
    return {"resolved": resolved, "failed": failed}


def read_checkpoint(path: Path) -> dict:
    """
    Returns {"branch": str|None, "checkpointed_ids": set[str], "is_complete": bool}.
    checkpointed_ids contains the IDs of all issues already recorded in the checkpoint.
    is_complete is True when a footer line exists (run finished successfully).
    """
    result: dict = {"branch": None, "checkpointed_ids": set(), "is_complete": False}
    if not path.exists():
        return result
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = rec.get("type")
            if t == "header":
                result["branch"] = rec.get("branch")
            elif t == "issue":
                result["checkpointed_ids"].add(rec.get("id", ""))
            elif t == "footer":
                result["is_complete"] = True
    return result
