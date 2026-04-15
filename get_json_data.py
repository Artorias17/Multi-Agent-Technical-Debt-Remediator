import sqlite3
import pandas as pd
import json
import math

def execute_query():
    with open("query.sql", "r") as f:
        sql = f.read()

    conn = sqlite3.connect("data/td_V2.db")
    df = pd.read_sql_query(sql, conn)
    df.rename(columns=str.lower, inplace=True)
    conn.close()
    
    return df

def clean(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if value == '':
        return None
    return value

def export_to_json(df: pd.DataFrame):
    measure_cols = [
        "sqale_index", "sqale_debt_ratio",
        "code_smells", "bugs", "vulnerabilities",
        "blocker_violations", "critical_violations",
        "major_violations", "minor_violations", "info_violations",
        "line_coverage",
        "sqale_rating", "security_rating", "reliability_rating",
        "cognitive_complexity", "cyclomatic_complexity"
    ]

    issue_cols = [
        "rule", "type", "severity",
        "component", "action_message",
        "start_line", "end_line",
        "effort_minutes", "description"
    ]

    for (proj, git), group in df.groupby(["project_id", "git_link"], dropna=False):
        dates = pd.to_datetime(group["creation_commit_date"])
        latest_row = group.loc[dates == dates.max()].iloc[0]

        report = {
            "project_id": proj,
            "git_link": git,
            "commit_hash": clean(latest_row["commit_hash"]),
            "measures": {
                col: clean(latest_row.get(col)) for col in measure_cols
            },
            "issues": [
                {col: clean(row[col]) for col in issue_cols}
                for _, row in group[issue_cols].iterrows()
            ]
        }

        with open(f"data/sonar_report_{report["project_id"]}.json", "w") as f:
            json.dump(report, f, indent=2, default=str)
            
        print(f"Written {report["project_id"]} projects")
        

if __name__ == "__main__":
    df = execute_query()
    export_to_json(df)