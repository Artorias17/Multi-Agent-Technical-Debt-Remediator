"""
run.py — invoke the TD Agent pipeline against a SonarQube report JSON.

Usage:
    python run.py --report data/sonar_report_<project>.json
    [--project-dir path_to_repo]
    (env vars are loaded from .env automatically)
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

from graph import graph
from state import initialize_pipeline_state
from checkpoint import get_project_key, checkpoint_path as make_checkpoint_path, read_checkpoint, write_resume_marker


def main():
    parser = argparse.ArgumentParser(
        description="Run TD Agent pipeline against a SonarQube report"
    )
    parser.add_argument("--report", required=True, help="Path to sonar_report_*.json")
    parser.add_argument(
        "--project-dir",
        default=None,
        help="Path to pre-cloned local repo (skips git clone)",
    )
    args = parser.parse_args()

    with open(args.report) as f:
        report = json.load(f)

    print(f"Project : {report.get('project_id', '?')}")
    print(f"Issues  : {len(report.get('issues', []))}")
    print(f"Commit  : {(report.get('commit_hash') or '')[:12] or '?'}")
    if args.project_dir:
        print(f"Repo    : {args.project_dir} (local)")
    print()

    project_key = get_project_key(report)
    ckpt_path = make_checkpoint_path(project_key)
    ckpt_data = read_checkpoint(ckpt_path)

    if ckpt_data["is_complete"]:
        print("Checkpoint shows a completed run — nothing to resume.")
        print(f"  Checkpoint: {ckpt_path}")
        sys.exit(0)

    initial_state = initialize_pipeline_state(report, project_dir=args.project_dir)
    initial_state["checkpoint_path"] = str(ckpt_path)

    if ckpt_data["branch"]:
        checkpointed = ckpt_data["checkpointed_ids"]
        print(f"Resuming branch : {ckpt_data['branch']}")
        print(f"Skipping        : {len(checkpointed)} already-checkpointed issue(s)")
        initial_state["resume_branch"] = ckpt_data["branch"]
        initial_state["checkpointed_issue_ids"] = list(checkpointed)
        write_resume_marker(ckpt_path, datetime.now(timezone.utc).isoformat())

    result = {}

    try:
        result = graph.invoke(initial_state)
    except Exception as exc:
        print(f"\nPipeline error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        # Only clean up temp clones — never delete the user's pre-existing directory
        if not args.project_dir:
            repo_path = result.get("repo_path")
            if repo_path:
                shutil.rmtree(repo_path, ignore_errors=True)

    approved = result.get("approved") or []
    failed = result.get("failed") or []
    pr_url = result.get("pr_url", "—")

    print()
    print("── Run complete " + "─" * 32)
    print(f"  Approved : {len(approved)} file(s)")
    print(f"  Failed   : {len(failed)} file(s)")
    print(f"  PR       : {pr_url}")
    print(f"  Checkpoint: {ckpt_path}")


if __name__ == "__main__":
    main()
