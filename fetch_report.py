"""
fetch_report.py — pull a SonarQube project scan into the pipeline JSON schema.

Usage:
    SONARQUBE_TOKEN=<token> python fetch_report.py \
        --project-key org.example:myproject \
        --git-link https://github.com/org/repo \
        [--sonar-url http://localhost:9000] \
        [--output data/my_report.json]
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

METRIC_KEYS = [
    "sqale_index", "sqale_debt_ratio",
    "code_smells", "bugs", "vulnerabilities",
    "blocker_violations", "critical_violations",
    "major_violations", "minor_violations", "info_violations",
    "line_coverage",
    "sqale_rating", "security_rating", "reliability_rating",
    "cognitive_complexity", "cyclomatic_complexity",
]


# ── HTTP helpers ─────────────────────────────────────────────

def _session(token: str) -> requests.Session:
    s = requests.Session()
    s.auth = (token, "")   # SonarQube token auth: token as username, empty password
    return s


def _get(session: requests.Session, base_url: str, path: str, params: dict = None) -> dict:
    url = base_url.rstrip("/") + path
    resp = session.get(url, params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ── Effort parsing ───────────────────────────────────────────

def _parse_effort(effort: str | None) -> int | None:
    """Parse SonarQube effort strings like '20min', '1h', '2h30min' → minutes."""
    if not effort:
        return None
    minutes = 0
    for val, unit in re.findall(r"(\d+)(h|min)", effort):
        minutes += int(val) * 60 if unit == "h" else int(val)
    return minutes or None


# ── API fetchers ─────────────────────────────────────────────

def _fetch_commit_hash(session: requests.Session, base_url: str, project_key: str) -> str | None:
    data = _get(session, base_url, "/api/project_analyses/search", {
        "project": project_key,
        "ps": 1,
    })
    analyses = data.get("analyses", [])
    if not analyses:
        return None
    return analyses[0].get("revision")


def _fetch_measures(session: requests.Session, base_url: str, project_key: str) -> dict:
    data = _get(session, base_url, "/api/measures/component", {
        "component": project_key,
        "metricKeys": ",".join(METRIC_KEYS),
    })
    measures_raw = data.get("component", {}).get("measures", [])
    measures = {}
    for m in measures_raw:
        key = m["metric"]
        val = m.get("value") or m.get("period", {}).get("value")
        try:
            val = float(val) if val is not None else None
        except (TypeError, ValueError):
            pass
        measures[key] = val
    # Fill any missing metric keys with None
    for key in METRIC_KEYS:
        measures.setdefault(key, None)
    return measures


def _fetch_all_issues(session: requests.Session, base_url: str, project_key: str) -> list[dict]:
    issues = []
    page = 1
    while True:
        data = _get(session, base_url, "/api/issues/search", {
            "componentKeys": project_key,
            "resolved": "false",
            "ps": 500,
            "p": page,
        })
        batch = data.get("issues", [])
        issues.extend(batch)
        paging = data.get("paging", {})
        total = paging.get("total", 0)
        fetched = paging.get("pageIndex", page) * paging.get("pageSize", 500)
        print(f"  Issues fetched: {len(issues)}/{total}", end="\r")
        if fetched >= total or not batch:
            break
        page += 1
    print()
    return issues


def _fetch_rule_descriptions(
    session: requests.Session, base_url: str, rule_keys: list[str]
) -> dict[str, str | None]:
    """Batch-fetch rule descriptions in groups of 100."""
    descriptions: dict[str, str | None] = {}
    for i in range(0, len(rule_keys), 100):
        batch = rule_keys[i : i + 100]
        try:
            data = _get(session, base_url, "/api/rules/search", {
                "rule_keys": ",".join(batch),
                "ps": 500,
                "fields": "htmlDesc",
            })
            for rule in data.get("rules", []):
                descriptions[rule["key"]] = rule.get("htmlDesc")
        except Exception as exc:
            print(f"  Warning: could not fetch rule descriptions for batch: {exc}")
    return descriptions


# ── Main ─────────────────────────────────────────────────────

def fetch_report(
    project_key: str,
    git_link: str,
    sonar_url: str,
    token: str,
) -> dict:
    session = _session(token)

    print(f"Fetching report for {project_key} from {sonar_url}")

    print("  → commit hash")
    commit_hash = _fetch_commit_hash(session, sonar_url, project_key)
    if not commit_hash:
        print("  Warning: could not determine commit hash from SonarQube analyses")

    print("  → measures")
    measures = _fetch_measures(session, sonar_url, project_key)

    print("  → issues")
    raw_issues = _fetch_all_issues(session, sonar_url, project_key)

    print("  → rule descriptions")
    unique_rules = list({i["rule"] for i in raw_issues if i.get("rule")})
    rule_descriptions = _fetch_rule_descriptions(session, sonar_url, unique_rules)

    issues = []
    for i in raw_issues:
        text_range = i.get("textRange") or {}
        issues.append({
            "rule":           i.get("rule"),
            "type":           i.get("type"),
            "severity":       i.get("severity"),
            "component":      i.get("component"),
            "action_message": i.get("message"),
            "start_line":     text_range.get("startLine"),
            "end_line":       text_range.get("endLine"),
            "effort_minutes": _parse_effort(i.get("effort")),
            "description":    rule_descriptions.get(i.get("rule", "")),
        })

    return {
        "project_id":  project_key,
        "git_link":    git_link,
        "commit_hash": commit_hash,
        "measures":    measures,
        "issues":      issues,
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch SonarQube report → pipeline JSON")
    parser.add_argument("--project-key", required=True, help="SonarQube project key")
    parser.add_argument("--git-link",    required=True, help="GitHub repo URL")
    parser.add_argument("--sonar-url",   default="http://localhost:9000", help="SonarQube base URL")
    parser.add_argument("--output",      default=None, help="Output JSON path (default: data/sonar_report_<key>.json)")
    args = parser.parse_args()

    token = os.environ.get("SONARQUBE_TOKEN", "")
    if not token:
        print("Error: SONARQUBE_TOKEN env var is not set", file=sys.stderr)
        sys.exit(1)

    report = fetch_report(args.project_key, args.git_link, args.sonar_url, token)

    output_path = args.output or f"data/sonar_report_{args.project_key}.json"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"Written {len(report['issues'])} issues → {output_path}")


if __name__ == "__main__":
    main()
