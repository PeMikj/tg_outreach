from __future__ import annotations

import json

from app.main import get_db


TEST_CHANNEL_PREFIXES = ("@smoke-", "@jobs-")


def main() -> None:
    connection = get_db()
    try:
        vacancy_rows = connection.execute(
            """
            SELECT id, recruiter_handle
            FROM vacancies
            WHERE source_channel LIKE ? OR source_channel LIKE ?
            """,
            (f"{TEST_CHANNEL_PREFIXES[0]}%", f"{TEST_CHANNEL_PREFIXES[1]}%"),
        ).fetchall()

        vacancy_ids = [str(row["id"]) for row in vacancy_rows]
        recruiter_handles = sorted({str(row["recruiter_handle"]) for row in vacancy_rows if row["recruiter_handle"]})

        conversation_ids: list[str] = []
        if recruiter_handles:
            placeholders = ",".join(["?"] * len(recruiter_handles))
            conversation_rows = connection.execute(
                f"SELECT id FROM conversations WHERE recruiter_handle IN ({placeholders})",
                tuple(recruiter_handles),
            ).fetchall()
            conversation_ids = [str(row["id"]) for row in conversation_rows]

        deleted: dict[str, int] = {
            "vacancies": 0,
            "approval_events": 0,
            "dispatch_events": 0,
            "outreach_attempts": 0,
            "audit_events": 0,
            "notification_events": 0,
            "memory_documents": 0,
            "conversation_summaries": 0,
            "inbound_message_events": 0,
            "jobs": 0,
            "conversations": 0,
            "recruiters": 0,
        }

        def delete_by_values(table: str, column: str, values: list[str]) -> None:
            if not values:
                return
            placeholders = ",".join(["?"] * len(values))
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
                tuple(values),
            )
            deleted[table] += int(cursor.rowcount or 0)

        delete_by_values("approval_events", "vacancy_id", vacancy_ids)
        delete_by_values("dispatch_events", "vacancy_id", vacancy_ids)
        delete_by_values("outreach_attempts", "vacancy_id", vacancy_ids)
        delete_by_values("conversation_summaries", "conversation_id", conversation_ids)
        delete_by_values("memory_documents", "entity_id", vacancy_ids + conversation_ids)
        delete_by_values("jobs", "entity_id", vacancy_ids + conversation_ids)
        delete_by_values("inbound_message_events", "recruiter_handle", recruiter_handles)
        delete_by_values("notification_events", "entity_id", vacancy_ids)

        if vacancy_ids:
            cursor = connection.execute(
                "DELETE FROM audit_events WHERE entity_type = ? AND entity_id IN ({})".format(
                    ",".join(["?"] * len(vacancy_ids))
                ),
                ("vacancy", *vacancy_ids),
            )
            deleted["audit_events"] += int(cursor.rowcount or 0)

        if conversation_ids:
            cursor = connection.execute(
                "DELETE FROM audit_events WHERE entity_type = ? AND entity_id IN ({})".format(
                    ",".join(["?"] * len(conversation_ids))
                ),
                ("conversation", *conversation_ids),
            )
            deleted["audit_events"] += int(cursor.rowcount or 0)

        delete_by_values("vacancies", "id", vacancy_ids)
        delete_by_values("conversations", "id", conversation_ids)
        delete_by_values("recruiters", "recruiter_handle", recruiter_handles)

        connection.commit()
        print(
            json.dumps(
                {
                    "status": "ok",
                    "matched_vacancies": len(vacancy_ids),
                    "matched_recruiters": len(recruiter_handles),
                    "matched_conversations": len(conversation_ids),
                    "deleted": deleted,
                }
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
