import asyncio
import json
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta

from app.main import (
    AGENT_EXECUTIONS,
    compute_expiry,
    fetch_latest_conversation_summary,
    refresh_conversation_memory,
    generate_follow_up_with_astrixa,
    get_db,
    get_emergency_stop_state,
    log_audit,
    poll_telegram_replies_internal,
    schedule_job,
    send_control_notification,
    settings,
    upsert_periodic_job,
)


WORKER_POLL_SECONDS = int(os.getenv("TG_OUTREACH_WORKER_POLL_SECONDS", "15"))
WORKER_LEASE_SECONDS = int(os.getenv("TG_OUTREACH_WORKER_LEASE_SECONDS", "60"))
WORKER_ID = f"{socket.gethostname()}-outreach-worker"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def lease_due_job():
    connection = get_db()
    emergency_stop = get_emergency_stop_state(connection)
    if emergency_stop.enabled:
        connection.close()
        return None

    now = utc_now()
    row = connection.execute(
        """
        SELECT * FROM jobs
        WHERE status = 'pending' AND run_at <= ?
        ORDER BY run_at ASC, created_at ASC
        LIMIT 1
        """,
        (now,),
    ).fetchone()
    if row is None:
        row = connection.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'leased' AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
            ORDER BY run_at ASC, created_at ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()
    if row is None:
        connection.close()
        return None

    lease_expires_at = compute_expiry(WORKER_LEASE_SECONDS)
    connection.execute(
        """
        UPDATE jobs
        SET status = 'leased', lease_owner = ?, lease_expires_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (WORKER_ID, lease_expires_at, now, row["id"]),
    )
    connection.commit()
    leased = connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
    connection.close()
    return leased


async def process_follow_up_due(job) -> None:
    connection = get_db()
    payload = json.loads(job["payload_json"] or "{}")
    conversation = connection.execute(
        "SELECT * FROM conversations WHERE id = ?",
        (job["entity_id"],),
    ).fetchone()
    if conversation is None:
        raise RuntimeError("conversation_not_found")

    if conversation["rejection_flag"] or conversation["last_inbound_at"]:
        connection.execute(
            "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (utc_now(), job["id"]),
        )
        log_audit(
            connection,
            entity_type="job_execution",
            entity_id=job["id"],
            event_type="follow_up_cancelled",
            payload={"reason": "conversation_already_responded_or_rejected"},
        )
        connection.commit()
        connection.close()
        AGENT_EXECUTIONS.labels(agent="execution_safety_agent", outcome="cancelled_follow_up").inc()
        return

    if conversation["follow_up_sent"]:
        connection.execute(
            "UPDATE jobs SET status = 'completed', updated_at = ? WHERE id = ?",
            (utc_now(), job["id"]),
        )
        connection.commit()
        connection.close()
        return

    recruiter_handle = payload.get("recruiter_handle") or conversation["recruiter_handle"]
    vacancy_id = payload.get("vacancy_id")
    vacancy = connection.execute(
        "SELECT * FROM vacancies WHERE id = ?",
        (vacancy_id,),
    ).fetchone()
    if vacancy is None:
        raise RuntimeError("vacancy_not_found")
    structured = json.loads(vacancy["structured_json"] or "{}")
    latest_summary = fetch_latest_conversation_summary(connection, str(conversation["id"]))
    follow_up_draft, follow_up_source = await generate_follow_up_with_astrixa(
        recruiter_handle=recruiter_handle,
        vacancy_title=structured.get("title") or vacancy["title"],
        company=structured.get("company"),
        previous_draft=vacancy["draft_text"],
        conversation_summary=latest_summary,
    )
    message = (
        f"[follow_up_due] Conversation with {recruiter_handle}\n"
        f"Vacancy: {vacancy_id}\n"
        f"Draft source: {follow_up_source}\n"
        f"Suggested follow-up: {follow_up_draft}"
    )
    await send_control_notification(
        event_type="follow_up_due",
        entity_type="conversation",
        entity_id=str(conversation["id"]),
        dedupe_key=f"follow_up_due:{conversation['id']}",
        payload={
            "vacancy_id": vacancy_id,
            "recruiter_handle": recruiter_handle,
            "draft_source": follow_up_source,
        },
        message=message,
    )
    connection.execute(
        """
        INSERT INTO outreach_attempts (
            id, vacancy_id, conversation_id, attempt_type, outcome, draft_text, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            vacancy_id,
            conversation["id"],
            "follow_up_draft",
            "pending_review",
            follow_up_draft,
            utc_now(),
        ),
    )
    connection.execute(
        "UPDATE conversations SET status = ?, follow_up_sent = ?, updated_at = ? WHERE id = ?",
        ("follow_up_pending_review", 1, utc_now(), conversation["id"]),
    )
    connection.execute(
        "UPDATE jobs SET status = 'completed', updated_at = ? WHERE id = ?",
        (utc_now(), job["id"]),
    )
    log_audit(
        connection,
        entity_type="job_execution",
        entity_id=job["id"],
        event_type="follow_up_due_processed",
        payload={"conversation_id": conversation["id"], "vacancy_id": vacancy_id},
    )
    connection.commit()
    connection.close()
    await refresh_conversation_memory(
        conversation_id=str(conversation["id"]),
        recruiter_handle=recruiter_handle,
        latest_message=follow_up_draft,
        latest_classification="follow_up_pending_review",
        source_event="follow_up_due",
    )
    AGENT_EXECUTIONS.labels(agent="execution_safety_agent", outcome="follow_up_due_processed").inc()


async def process_job(job) -> None:
    try:
        if job["job_type"] == "follow_up_due":
            await process_follow_up_due(job)
            return
        if job["job_type"] == "telegram_reply_poll":
            payload = json.loads(job["payload_json"] or "{}")
            result = await poll_telegram_replies_internal(int(payload.get("per_conversation_limit", 5)))
            connection = get_db()
            schedule_job(
                connection,
                job_type="telegram_reply_poll",
                entity_id="global",
                run_at=compute_expiry(settings.telegram_reply_poll_interval_seconds),
                payload={"per_conversation_limit": int(payload.get("per_conversation_limit", 5))},
                max_attempts=3,
            )
            connection.execute(
                "UPDATE jobs SET status = 'completed', updated_at = ? WHERE id = ?",
                (utc_now(), job["id"]),
            )
            log_audit(
                connection,
                entity_type="job_execution",
                entity_id=job["id"],
                event_type="telegram_reply_poll_completed",
                payload=result.model_dump(),
            )
            connection.commit()
            connection.close()
            AGENT_EXECUTIONS.labels(agent="ingestion_agent", outcome="telegram_reply_poll_completed").inc()
            return
        connection = get_db()
        connection.execute(
            "UPDATE jobs SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
            ("unsupported_job_type", utc_now(), job["id"]),
        )
        connection.commit()
        connection.close()
    except Exception as exc:
        connection = get_db()
        attempts = int(job["attempts"]) + 1
        status = "pending" if attempts < int(job["max_attempts"]) else "failed"
        next_run_at = compute_expiry(min(300, 15 * attempts))
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, attempts = ?, last_error = ?, run_at = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (status, attempts, str(exc), next_run_at, utc_now(), job["id"]),
        )
        log_audit(
            connection,
            entity_type="job_execution",
            entity_id=job["id"],
            event_type="job_failed",
            payload={"error": str(exc), "attempts": attempts},
        )
        connection.commit()
        connection.close()
        AGENT_EXECUTIONS.labels(agent="execution_safety_agent", outcome="job_failed").inc()


async def main() -> None:
    connection = get_db()
    upsert_periodic_job(
        connection,
        job_type="telegram_reply_poll",
        entity_id="global",
        interval_seconds=settings.telegram_reply_poll_interval_seconds,
        payload={"per_conversation_limit": 5},
        max_attempts=3,
    )
    connection.commit()
    connection.close()
    while True:
        job = lease_due_job()
        if job is None:
            await asyncio.sleep(WORKER_POLL_SECONDS)
            continue
        await process_job(job)


if __name__ == "__main__":
    asyncio.run(main())
