"""
stratify.py — Create a stratified sample from a SonarQube report.

Stratification: by issue type first, then by severity within each type.
Each type gets a minimum guaranteed allocation; remainder is distributed
proportionally. Within each type, severity allocation is proportional.

Filters: TS/JS files only, excluding test/spec/cypress files.
Output: same JSON schema as input, ready to pass to run.py.

Usage:
    python stratify.py --report data/sonar_report_mermaid.json
    python stratify.py --report data/sonar_report_mermaid.json --sample-size 100 --min-per-type 15 --seed 42
"""

import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

SEVERITY_ORDER = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]
TYPE_ORDER = ["BUG", "VULNERABILITY", "CODE_SMELL"]
TEST_PATTERNS = ["spec", "test", "__tests__", "cypress", ".test.", ".spec."]
TARGET_EXTENSIONS = {".ts", ".js"}
MAX_PER_FILE = 5


def is_test_file(file_path: str) -> bool:
    file_path_lower = file_path.lower()
    return any(pattern in file_path_lower for pattern in TEST_PATTERNS)


def allocate_proportional(counts: dict, total: int) -> dict:
    """Proportionally allocate `total` slots across buckets, ensuring each gets at least 1."""
    pool_total = sum(counts.values())
    allocation = {}
    for key, n in counts.items():
        allocation[key] = max(1, round(n / pool_total * total))
    # Adjust to hit exactly total
    diff = total - sum(allocation.values())
    if diff != 0:
        largest = max(allocation, key=lambda k: allocation.get(k, 0))
        allocation[largest] += diff
    return allocation


def sample_stratum(pool: list, n: int, file_counts: dict) -> list:
    """Sample up to n issues from pool respecting per-file cap."""
    selected = []
    for issue in pool:
        component = issue.get("component", "")
        if file_counts[component] < MAX_PER_FILE:
            selected.append(issue)
            file_counts[component] += 1
        if len(selected) >= n:
            break
    return selected


def main():
    parser = argparse.ArgumentParser(
        description="Hierarchical stratified sample from SonarQube report"
    )
    parser.add_argument("--report", required=True, help="Path to sonar_report_*.json")
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument(
        "--min-per-type",
        type=int,
        default=15,
        help="Minimum issues guaranteed per type (if pool allows)",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    with open(args.report) as f:
        report = json.load(f)

    all_issues = report.get("issues", [])

    # ── Filter to TS/JS non-test ──────────────────────────────
    filtered = []
    for issue in all_issues:
        file_path = issue.get("component", "").split(":")[-1]
        if Path(file_path).suffix.lower() not in TARGET_EXTENSIONS:
            continue
        if is_test_file(file_path):
            continue
        filtered.append(issue)

    print(f"Total issues        : {len(all_issues)}")
    print(f"After TS/JS filter  : {len(filtered)}")

    # ── Group by type → severity ──────────────────────────────
    by_type_sev: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for issue in filtered:
        t = issue.get("type", "CODE_SMELL")
        s = issue.get("severity", "INFO")
        by_type_sev[t][s].append(issue)

    types_present = [t for t in TYPE_ORDER if t in by_type_sev]

    # ── Type-level allocation (with minimum floor) ────────────
    type_pool_sizes = {
        t: sum(len(v) for v in by_type_sev[t].values()) for t in types_present
    }
    type_allocation: dict[str, int] = {}

    # Guarantee minimum per type (capped at pool size)
    for t in types_present:
        type_allocation[t] = min(args.min_per_type, type_pool_sizes[t])

    guaranteed_total = sum(type_allocation.values())
    remaining = args.sample_size - guaranteed_total

    if remaining > 0:
        # Distribute remainder proportionally by pool size
        remainder_counts = {t: type_pool_sizes[t] for t in types_present}
        remainder_alloc = allocate_proportional(remainder_counts, remaining)
        for t in types_present:
            type_allocation[t] += remainder_alloc[t]

    # Adjust to hit exactly sample_size
    diff = args.sample_size - sum(type_allocation.values())
    if diff != 0:
        largest = max(type_allocation, key=lambda k: type_allocation.get(k, 0))
        type_allocation[largest] += diff

    print(
        f"\nType allocation (target {args.sample_size}, min/type {args.min_per_type}):"
    )
    for t in types_present:
        print(f"  {t:15s}: {type_allocation[t]:3d}  (pool: {type_pool_sizes[t]})")

    # ── Within each type: proportional by severity ────────────
    random.seed(args.seed)
    sampled = []
    file_counts: dict[str, int] = defaultdict(int)

    print("\nSeverity allocation within each type:")
    for t in types_present:
        sev_pool_sizes = {s: len(by_type_sev[t][s]) for s in by_type_sev[t]}
        sev_allocation = allocate_proportional(sev_pool_sizes, type_allocation[t])

        print(f"  {t}:")
        for s in SEVERITY_ORDER:
            if s in sev_allocation:
                print(
                    f"    {s:10s}: {sev_allocation[s]:3d}  (pool: {sev_pool_sizes[s]})"
                )

        for s in SEVERITY_ORDER:
            if s not in sev_allocation:
                continue
            pool = list(by_type_sev[t][s])
            random.shuffle(pool)
            selected = sample_stratum(pool, sev_allocation[s], file_counts)
            sampled.extend(selected)

    # ── Report ────────────────────────────────────────────────
    type_dist = Counter(i.get("type") for i in sampled)
    sev_dist = Counter(i.get("severity") for i in sampled)
    ext_dist = Counter(
        Path(i.get("component", "").split(":")[-1]).suffix.lower() for i in sampled
    )

    print(f"\nSample size         : {len(sampled)}")
    print(f"Unique files        : {len({i.get('component') for i in sampled})}")
    print("\nType distribution:")
    for t in TYPE_ORDER:
        if t in type_dist:
            print(
                f"  {t:15s}: {type_dist[t]:3d}  ({type_dist[t]/len(sampled)*100:.1f}%)"
            )
    print("\nSeverity distribution:")
    for s in SEVERITY_ORDER:
        if s in sev_dist:
            print(f"  {s:10s}: {sev_dist[s]:3d}  ({sev_dist[s]/len(sampled)*100:.1f}%)")
    print("\nLanguage distribution:")
    for ext, n in ext_dist.most_common():
        print(f"  {ext:6s}: {n}")

    # ── Write output ──────────────────────────────────────────
    output_report = {**report, "issues": sampled}
    output_path = (
        args.output
        or f"data/sonar_report_{report.get('project_id', 'sample')}_stratified.json"
    )
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        json.dump(output_report, f, indent=2)

    print(f"\nWritten → {output_path}")


if __name__ == "__main__":
    main()
