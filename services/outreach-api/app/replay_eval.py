import argparse
import json
from pathlib import Path
from typing import Any

from app.main import explain_vacancy, get_db, parse_vacancy, settings


def load_rows(limit: int | None) -> list[dict[str, Any]]:
    connection = get_db()
    query = "SELECT * FROM vacancies ORDER BY created_at DESC"
    params: tuple[Any, ...] = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)
    rows = connection.execute(query, params).fetchall()
    connection.close()
    return rows


def compare_lists(left: list[str], right: list[str]) -> bool:
    return sorted(left) == sorted(right)


def build_record_diff(row: dict[str, Any]) -> dict[str, Any]:
    stored_structured = json.loads(row["structured_json"] or "{}")
    stored_breakdown = json.loads(row["score_breakdown_json"] or "{}")
    stored_reasons = json.loads(row["filter_reasons_json"] or "[]")

    replay_structured = parse_vacancy(row["raw_text"], row["recruiter_handle"])
    replay_score, replay_matched_skills, replay_breakdown, replay_decision, replay_reasons = explain_vacancy(
        replay_structured,
        row["raw_text"],
    )

    diff: dict[str, Any] = {
        "id": row["id"],
        "source_channel": row["source_channel"],
        "title": row["title"],
        "status": row["status"],
        "changed_fields": [],
        "stored": {
            "title": stored_structured.get("title"),
            "company": stored_structured.get("company"),
            "location": stored_structured.get("location"),
            "work_mode": stored_structured.get("work_mode"),
            "salary_min": stored_structured.get("salary_min"),
            "salary_max": stored_structured.get("salary_max"),
            "currency": stored_structured.get("currency"),
            "salary_type": stored_structured.get("salary_type"),
            "skills": stored_structured.get("skills", []),
            "detected_roles": stored_structured.get("detected_roles", []),
            "filter_decision": row["filter_decision"],
            "filter_reasons": stored_reasons,
            "score": row["score"],
            "matched_skills": stored_breakdown.get("matched_skills", []),
        },
        "replay": {
            "title": replay_structured.get("title"),
            "company": replay_structured.get("company"),
            "location": replay_structured.get("location"),
            "work_mode": replay_structured.get("work_mode"),
            "salary_min": replay_structured.get("salary_min"),
            "salary_max": replay_structured.get("salary_max"),
            "currency": replay_structured.get("currency"),
            "salary_type": replay_structured.get("salary_type"),
            "skills": replay_structured.get("skills", []),
            "detected_roles": replay_structured.get("detected_roles", []),
            "filter_decision": replay_decision,
            "filter_reasons": replay_reasons,
            "score": replay_score,
            "matched_skills": replay_matched_skills,
        },
    }

    scalar_fields = [
        "title",
        "company",
        "location",
        "work_mode",
        "salary_min",
        "salary_max",
        "currency",
        "salary_type",
        "filter_decision",
    ]
    for field in scalar_fields:
        if diff["stored"][field] != diff["replay"][field]:
            diff["changed_fields"].append(field)

    list_fields = ["skills", "detected_roles", "filter_reasons", "matched_skills"]
    for field in list_fields:
        if not compare_lists(diff["stored"][field], diff["replay"][field]):
            diff["changed_fields"].append(field)

    stored_score = float(diff["stored"]["score"] or 0.0)
    replay_score_value = float(diff["replay"]["score"] or 0.0)
    if abs(stored_score - replay_score_value) >= 0.0001:
        diff["changed_fields"].append("score")

    return diff


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay parser/policy eval on stored vacancies")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output", default=None, help="Optional path to write JSON report")
    parser.add_argument("--show", type=int, default=10, help="How many changed records to print")
    args = parser.parse_args()

    rows = load_rows(args.limit)
    diffs = [build_record_diff(row) for row in rows]
    changed = [diff for diff in diffs if diff["changed_fields"]]

    field_change_counts: dict[str, int] = {}
    for diff in changed:
        for field in diff["changed_fields"]:
            field_change_counts[field] = field_change_counts.get(field, 0) + 1

    summary = {
        "scanned": len(diffs),
        "changed": len(changed),
        "unchanged": len(diffs) - len(changed),
        "field_change_counts": dict(sorted(field_change_counts.items(), key=lambda item: (-item[1], item[0]))),
        "sample_changed": changed[: args.show],
    }

    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.output:
        Path(args.output).write_text(json.dumps({"summary": summary, "diffs": diffs}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
