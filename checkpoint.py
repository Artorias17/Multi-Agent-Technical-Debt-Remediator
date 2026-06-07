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
