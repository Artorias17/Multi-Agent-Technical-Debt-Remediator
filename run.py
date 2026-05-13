"""
run.py — invoke the TD Agent pipeline against a SonarQube report JSON.

Usage:
    GITHUB_TOKEN=<token> python run.py --report data/sonar_report_<project>.json
"""

import argparse
import json
import shutil
import sys

from graph import graph
from state import initialize_pipeline_state


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

    initial_state = initialize_pipeline_state(report, project_dir=args.project_dir)
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


if __name__ == "__main__":
    main()
