import asyncio
import json
import os
import re
import smtplib
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field
from psycopg.rows import dict_row
from telethon import TelegramClient
from telethon.sessions import StringSession


REQUEST_COUNT = Counter(
    "tg_outreach_requests_total",
    "Total outreach API requests",
    ["endpoint", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "tg_outreach_request_latency_seconds",
    "Outreach API request latency",
    ["endpoint"],
)
ASTRIXA_CALLS = Counter(
    "tg_outreach_astrixa_calls_total",
    "Astrixa call outcomes",
    ["outcome"],
)
NOTIFICATION_CALLS = Counter(
    "tg_outreach_notifications_total",
    "Operator notification outcomes",
    ["outcome"],
)
AGENT_EXECUTIONS = Counter(
    "tg_outreach_agent_executions_total",
    "Agent execution outcomes",
    ["agent", "outcome"],
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Settings:
    astrixa_base_url: str = os.getenv("TG_OUTREACH_ASTRIXA_BASE_URL", "http://127.0.0.1:18080").rstrip("/")
    astrixa_model: str = os.getenv("TG_OUTREACH_ASTRIXA_MODEL", "mock-1")
    astrixa_token: str = os.getenv("TG_OUTREACH_ASTRIXA_TOKEN", "astrixa-dev-token")
    database_url: str = os.getenv("TG_OUTREACH_DATABASE_URL", "").strip()
    build_version: str = os.getenv("TG_OUTREACH_BUILD_VERSION", "dev")
    git_sha: str = os.getenv("TG_OUTREACH_GIT_SHA", "unknown")
    user_headline: str = os.getenv(
        "TG_OUTREACH_USER_HEADLINE",
        "Senior Python/ML engineer focused on backend systems and MLOps",
    )
    user_skills_raw: str = os.getenv(
        "TG_OUTREACH_USER_SKILLS",
        "python,fastapi,backend,mlops,llm,infra,docker,postgresql,observability",
    )
    min_score: float = float(os.getenv("TG_OUTREACH_MIN_SCORE", "0.20"))
    max_daily_outreach: int = int(os.getenv("TG_OUTREACH_MAX_DAILY_OUTREACH", "3"))
    astrixa_timeout_seconds: float = float(os.getenv("TG_OUTREACH_ASTRIXA_TIMEOUT_SECONDS", "8"))
    profile_path: str = os.getenv("TG_OUTREACH_PROFILE_PATH", "./config/candidate_profile.json")
    preferences_path: str = os.getenv("TG_OUTREACH_PREFERENCES_PATH", "./config/candidate_preferences.json")
    telegram_api_id_raw: str = os.getenv("TG_OUTREACH_TELEGRAM_API_ID", "").strip()
    telegram_api_hash: str = os.getenv("TG_OUTREACH_TELEGRAM_API_HASH", "").strip()
    telegram_session_string: str = os.getenv("TG_OUTREACH_TELEGRAM_SESSION_STRING", "").strip()
    telegram_channels_raw: str = os.getenv(
        "TG_OUTREACH_TELEGRAM_CHANNELS",
        "t.me/hrlunapark,t.me/yuniorapp,t.me/dev_connectablejobs",
    )
    smtp_host: str = os.getenv("TG_OUTREACH_SMTP_HOST", "").strip()
    smtp_port: int = int(os.getenv("TG_OUTREACH_SMTP_PORT", "587"))
    smtp_username: str = os.getenv("TG_OUTREACH_SMTP_USERNAME", "").strip()
    smtp_password: str = os.getenv("TG_OUTREACH_SMTP_PASSWORD", "").strip()
    smtp_from_email: str = os.getenv("TG_OUTREACH_SMTP_FROM_EMAIL", "").strip()
    smtp_starttls: bool = os.getenv("TG_OUTREACH_SMTP_STARTTLS", "true").strip().lower() != "false"
    notify_target: str = os.getenv("TG_OUTREACH_NOTIFY_TARGET", "").strip()
    dispatch_mode: str = os.getenv("TG_OUTREACH_DISPATCH_MODE", "dry_run").strip().lower()
    context_budget_tokens: int = int(os.getenv("TG_OUTREACH_CONTEXT_BUDGET_TOKENS", "3000"))
    approval_ttl_seconds: int = int(os.getenv("TG_OUTREACH_APPROVAL_TTL_SECONDS", "86400"))
    follow_up_delay_seconds: int = int(os.getenv("TG_OUTREACH_FOLLOW_UP_DELAY_SECONDS", "259200"))
    worker_poll_seconds: int = int(os.getenv("TG_OUTREACH_WORKER_POLL_SECONDS", "15"))
    telegram_reply_poll_interval_seconds: int = int(os.getenv("TG_OUTREACH_TELEGRAM_REPLY_POLL_INTERVAL_SECONDS", "300"))

    @property
    def user_skills(self) -> list[str]:
        return [item.strip().lower() for item in self.user_skills_raw.split(",") if item.strip()]

    @property
    def telegram_api_id(self) -> int | None:
        if not self.telegram_api_id_raw:
            return None
        return int(self.telegram_api_id_raw)

    @property
    def telegram_channels(self) -> list[str]:
        return [item.strip() for item in self.telegram_channels_raw.split(",") if item.strip()]


settings = Settings()
app = FastAPI(title="TG Outreach PoC API", version="0.1.0")
STATIC_DIR = Path(__file__).with_name("static")
SQL_DIR = Path(__file__).with_name("sql")
DB_SCHEMA_INITIALIZED = False


class RuntimeValidationError(RuntimeError):
    pass


class PostgresConnection:
    def __init__(self, connection: psycopg.Connection) -> None:
        self._connection = connection

    def execute(self, query: str, params: tuple[Any, ...] | list[Any] = ()) -> psycopg.Cursor:
        cursor = self._connection.cursor()
        cursor.execute(query.replace("?", "%s"), params)
        return cursor

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


def get_existing_columns(connection: Any, table_name: str) -> set[str]:
    return {
        str(row["column_name"])
        for row in connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ?
            """,
            (table_name,),
        ).fetchall()
    }


def telegram_runtime_configured() -> bool:
    return bool(settings.telegram_api_id and settings.telegram_api_hash and settings.telegram_session_string)


def smtp_runtime_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from_email)


def validate_runtime_config() -> None:
    errors: list[str] = []

    if not settings.database_url:
        errors.append("TG_OUTREACH_DATABASE_URL is required")

    if settings.dispatch_mode not in {"dry_run", "manual_send"}:
        errors.append("TG_OUTREACH_DISPATCH_MODE must be one of: dry_run, manual_send")

    if settings.context_budget_tokens <= 0:
        errors.append("TG_OUTREACH_CONTEXT_BUDGET_TOKENS must be > 0")

    if settings.max_daily_outreach <= 0:
        errors.append("TG_OUTREACH_MAX_DAILY_OUTREACH must be > 0")

    if settings.approval_ttl_seconds <= 0:
        errors.append("TG_OUTREACH_APPROVAL_TTL_SECONDS must be > 0")

    if settings.follow_up_delay_seconds <= 0:
        errors.append("TG_OUTREACH_FOLLOW_UP_DELAY_SECONDS must be > 0")

    if settings.worker_poll_seconds <= 0:
        errors.append("TG_OUTREACH_WORKER_POLL_SECONDS must be > 0")

    if settings.telegram_reply_poll_interval_seconds <= 0:
        errors.append("TG_OUTREACH_TELEGRAM_REPLY_POLL_INTERVAL_SECONDS must be > 0")

    if settings.notify_target and not telegram_runtime_configured():
        errors.append("TG_OUTREACH_NOTIFY_TARGET requires Telegram runtime credentials")

    if settings.smtp_username and not settings.smtp_password:
        errors.append("TG_OUTREACH_SMTP_PASSWORD is required when TG_OUTREACH_SMTP_USERNAME is set")

    if settings.smtp_password and not settings.smtp_username:
        errors.append("TG_OUTREACH_SMTP_USERNAME is required when TG_OUTREACH_SMTP_PASSWORD is set")

    if settings.dispatch_mode == "manual_send" and not (telegram_runtime_configured() or smtp_runtime_configured()):
        errors.append("manual_send requires Telegram runtime credentials or SMTP runtime configuration")

    if errors:
        raise RuntimeValidationError("; ".join(errors))


class VacancyIngestRequest(BaseModel):
    source_channel: str
    recruiter_handle: str | None = None
    vacancy_text: str = Field(min_length=20)


class VacancyRecord(BaseModel):
    id: str
    source_channel: str
    recruiter_handle: str | None = None
    contact_email: str | None = None
    title: str
    status: str
    score: float
    score_breakdown: dict[str, Any]
    filter_decision: str
    filter_reasons: list[str]
    draft_text: str
    raw_text: str
    structured_data: dict[str, Any]
    context_bundle: dict[str, Any]
    approval_expires_at: str | None = None
    created_at: str
    updated_at: str


class VacancyIngestResult(BaseModel):
    source_channel: str
    input_chunks: int
    created_count: int
    duplicate_count: int
    created: list[VacancyRecord]


class DispatchRequest(BaseModel):
    operator: str = "local-operator"
    note: str | None = None


class TelegramIngestRequest(BaseModel):
    per_channel_limit: int = Field(default=5, ge=1, le=20)


class TelegramIngestResult(BaseModel):
    configured_channels: list[str]
    processed_channels: int
    fetched_messages: int
    created_vacancies: int
    skipped_duplicates: int


class ApprovalRequest(BaseModel):
    operator: str = "local-operator"
    note: str | None = None
    edited_draft: str | None = None


class QueueDispatchRequest(BaseModel):
    operator: str = "local-operator"
    note: str | None = None


class DashboardSummary(BaseModel):
    total_vacancies: int
    by_status: dict[str, int]
    by_filter_decision: dict[str, int]
    top_channels: dict[str, int]


class ReviewGroup(BaseModel):
    status: str
    items: list[VacancyRecord]


class ReviewBoard(BaseModel):
    groups: list[ReviewGroup]


class BackfillResult(BaseModel):
    scanned: int
    updated: int
    skipped: int


class EmergencyStopRequest(BaseModel):
    operator: str = "local-operator"
    enabled: bool
    reason: str | None = None


class EmergencyStopState(BaseModel):
    enabled: bool
    reason: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None


class ConversationRecord(BaseModel):
    id: str
    recruiter_handle: str
    status: str
    last_outbound_at: str | None = None
    last_inbound_at: str | None = None
    rejection_flag: bool
    follow_up_sent: bool
    created_at: str
    updated_at: str


class RecruiterRecord(BaseModel):
    id: str
    recruiter_handle: str
    status: str
    first_seen_at: str
    last_seen_at: str
    last_outbound_at: str | None = None
    last_inbound_at: str | None = None
    outbound_count: int
    inbound_count: int
    positive_reply_count: int
    negative_reply_count: int
    notes: str | None = None
    updated_at: str


class ConversationSummaryRecord(BaseModel):
    id: str
    conversation_id: str
    summary_text: str
    source_event: str
    created_at: str


class MemoryDocumentRecord(BaseModel):
    id: str
    memory_type: str
    entity_id: str
    content_text: str
    metadata: dict[str, Any]
    created_at: str


class ConversationReplyRequest(BaseModel):
    recruiter_handle: str
    message_text: str = Field(min_length=1)
    source: str = "telegram_inbound"


class ConversationReplyResult(BaseModel):
    conversation: ConversationRecord
    classification: str
    cancelled_jobs: int


class TelegramReplyPollRequest(BaseModel):
    per_conversation_limit: int = Field(default=5, ge=1, le=20)


class TelegramReplyPollResult(BaseModel):
    processed_conversations: int
    fetched_messages: int
    ingested_replies: int
    skipped_duplicates: int


class WorkerSeedResult(BaseModel):
    seeded_jobs: list[str]


class JobRecord(BaseModel):
    id: str
    job_type: str
    entity_id: str
    status: str
    run_at: str
    attempts: int
    max_attempts: int
    payload: dict[str, Any]
    last_error: str | None = None
    created_at: str
    updated_at: str


class JobFailureRecord(BaseModel):
    id: str
    job_type: str
    entity_id: str
    attempts: int
    max_attempts: int
    run_at: str
    last_error: str | None = None
    updated_at: str


class OpsSummary(BaseModel):
    total_jobs: int
    jobs_by_status: dict[str, int]
    jobs_by_type: dict[str, int]
    due_pending_jobs: int
    overdue_leased_jobs: int
    failed_jobs: int
    oldest_pending_job_age_seconds: int | None = None
    total_recruiters: int
    recruiters_by_status: dict[str, int]
    contacted_without_reply: int
    total_conversations: int
    conversations_by_status: dict[str, int]
    contact_extraction_by_status: dict[str, int]
    contact_extraction_by_reason: dict[str, int]
    generation_sources: dict[str, int]
    fallback_generations: int
    worker_status: str
    worker_heartbeat_age_seconds: int | None = None
    astrixa_health_status: str
    astrixa_invoke_status: str
    dependency_degraded: bool


class TimelineEventRecord(BaseModel):
    source: str
    event_type: str
    entity_id: str
    created_at: str
    payload: dict[str, Any]


class RecruiterOverview(BaseModel):
    recruiter: RecruiterRecord
    conversation: ConversationRecord | None = None
    vacancies: list[VacancyRecord]
    conversation_summaries: list[ConversationSummaryRecord]
    outreach_attempts: list[dict[str, Any]]
    dispatch_events: list[dict[str, Any]]
    timeline: list[TimelineEventRecord]


class ConversationTimelineItem(BaseModel):
    kind: str
    created_at: str
    direction: str | None = None
    source: str | None = None
    summary: str
    details: dict[str, Any]


def load_json_file(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_sql_file(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def execute_sql_script(connection: Any, sql_script: str) -> None:
    for statement in sql_script.split(";"):
        normalized = statement.strip()
        if not normalized:
            continue
        connection.execute(normalized)


def ensure_schema_migrations_table(connection: Any) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )


def get_applied_migrations(connection: Any) -> list[str]:
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version ASC"
    ).fetchall()
    return [str(row["version"]) for row in rows]


def apply_sql_migrations(connection: Any) -> None:
    ensure_schema_migrations_table(connection)
    applied = set(get_applied_migrations(connection))
    for path in sorted(SQL_DIR.glob("*.sql")):
        version = path.name
        if version in applied:
            continue
        execute_sql_script(connection, load_sql_file(version))
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, utc_now()),
        )


CANDIDATE_PROFILE = load_json_file(settings.profile_path)
CANDIDATE_PREFERENCES = load_json_file(settings.preferences_path)


def initialize_db_schema(connection: Any) -> None:
    global DB_SCHEMA_INITIALIZED
    apply_sql_migrations(connection)
    existing_columns = get_existing_columns(connection, "vacancies")
    if "score_breakdown_json" not in existing_columns:
        connection.execute("ALTER TABLE vacancies ADD COLUMN score_breakdown_json TEXT NOT NULL DEFAULT '{}'")
    if "filter_decision" not in existing_columns:
        connection.execute("ALTER TABLE vacancies ADD COLUMN filter_decision TEXT NOT NULL DEFAULT 'manual_review'")
    if "filter_reasons_json" not in existing_columns:
        connection.execute("ALTER TABLE vacancies ADD COLUMN filter_reasons_json TEXT NOT NULL DEFAULT '[]'")
    if "context_bundle_json" not in existing_columns:
        connection.execute("ALTER TABLE vacancies ADD COLUMN context_bundle_json TEXT NOT NULL DEFAULT '{}'")
    if "approval_expires_at" not in existing_columns:
        connection.execute("ALTER TABLE vacancies ADD COLUMN approval_expires_at TEXT")
    dispatch_columns = get_existing_columns(connection, "dispatch_events")
    if "contact_channel" not in dispatch_columns:
        connection.execute("ALTER TABLE dispatch_events ADD COLUMN contact_channel TEXT")
    if "contact_target" not in dispatch_columns:
        connection.execute("ALTER TABLE dispatch_events ADD COLUMN contact_target TEXT")
    connection.commit()
    DB_SCHEMA_INITIALIZED = True


def get_db() -> Any:
    if not settings.database_url:
        raise RuntimeError("TG_OUTREACH_DATABASE_URL must be set")
    raw_connection = psycopg.connect(settings.database_url, row_factory=dict_row)
    connection: Any = PostgresConnection(raw_connection)
    if not DB_SCHEMA_INITIALIZED:
        initialize_db_schema(connection)
    return connection


def database_backend_name() -> str:
    return "postgres"


def get_control_state_value(connection: Any, key: str) -> tuple[dict[str, Any] | None, str | None]:
    row = connection.execute(
        "SELECT value_json, updated_at FROM control_state WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None, None
    return json.loads(row["value_json"]), row["updated_at"]


def set_control_state_value(connection: Any, *, key: str, value: dict[str, Any]) -> str:
    updated_at = utc_now()
    connection.execute(
        """
        INSERT INTO control_state (key, value_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
        """,
        (key, json.dumps(value), updated_at),
    )
    return updated_at


def log_audit(
    connection: Any,
    *,
    entity_type: str,
    entity_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO audit_events (id, entity_type, entity_id, event_type, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (str(uuid.uuid4()), entity_type, entity_id, event_type, json.dumps(payload), utc_now()),
    )


def normalize_words(text: str) -> set[str]:
    cleaned = []
    for char in text.lower():
        cleaned.append(char if char.isalnum() else " ")
    return {token for token in "".join(cleaned).split() if len(token) >= 3}


def estimate_tokens(text: str) -> int:
    return max(1, len(text.split()))


def get_emergency_stop_state(connection: Any) -> EmergencyStopState:
    value, updated_at = get_control_state_value(connection, "emergency_stop")
    if value is None:
        return EmergencyStopState(enabled=False, reason=None, updated_at=None, updated_by=None)
    return EmergencyStopState(
        enabled=bool(value.get("enabled", False)),
        reason=value.get("reason"),
        updated_at=updated_at,
        updated_by=value.get("updated_by"),
    )


def set_emergency_stop_state(
    connection: Any,
    *,
    enabled: bool,
    operator: str,
    reason: str | None,
) -> EmergencyStopState:
    payload = {
        "enabled": enabled,
        "reason": reason,
        "updated_by": operator,
    }
    updated_at = set_control_state_value(connection, key="emergency_stop", value=payload)
    return EmergencyStopState(
        enabled=enabled,
        reason=reason,
        updated_at=updated_at,
        updated_by=operator,
    )


def assert_emergency_stop_not_enabled(connection: Any, action: str) -> None:
    state = get_emergency_stop_state(connection)
    if state.enabled:
        raise HTTPException(
            status_code=409,
            detail=f"Emergency stop is enabled; action '{action}' is blocked",
        )


def compute_expiry(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def is_expired(iso_timestamp: str | None) -> bool:
    if not iso_timestamp:
        return False
    return datetime.fromisoformat(iso_timestamp) <= datetime.now(UTC)


def age_seconds_from_iso(iso_timestamp: str | None) -> int | None:
    if not iso_timestamp:
        return None
    try:
        delta = datetime.now(UTC) - datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return None
    return max(0, int(delta.total_seconds()))


def probe_astrixa_health() -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = httpx.get(f"{settings.astrixa_base_url}/healthz", timeout=3.0)
        response.raise_for_status()
        return {
            "status": "ok",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
    except httpx.HTTPError as exc:
        return {
            "status": "error",
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error_type": exc.__class__.__name__,
        }


def probe_astrixa_invoke() -> dict[str, Any]:
    started = time.perf_counter()
    headers = {
        "authorization": f"Bearer {settings.astrixa_token}",
        "content-type": "application/json",
    }
    payload = {
        "model": settings.astrixa_model,
        "messages": [{"role": "user", "content": "Reply with the single word ok."}],
        "metadata": {
            "project": "tg_outreach_poc",
            "workflow": "dependency_probe",
            "anonymization_mode": "off",
            "anonymization_profile": "none",
        },
    }
    try:
        response = httpx.post(
            f"{settings.astrixa_base_url}/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=min(settings.astrixa_timeout_seconds, 5.0),
        )
        response.raise_for_status()
        data = response.json()
        output_text = str(data.get("output_text", "")).strip()
        if not output_text:
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                first_choice = choices[0] or {}
                message = first_choice.get("message") or {}
                content = message.get("content")
                if isinstance(content, str):
                    output_text = content.strip()
        return {
            "status": "ok" if output_text else "degraded",
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "response_present": bool(output_text),
        }
    except httpx.HTTPError as exc:
        return {
            "status": "error",
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error_type": exc.__class__.__name__,
        }


def conversation_from_row(row: dict[str, Any]) -> ConversationRecord:
    return ConversationRecord(
        id=row["id"],
        recruiter_handle=row["recruiter_handle"],
        status=row["status"],
        last_outbound_at=row["last_outbound_at"],
        last_inbound_at=row["last_inbound_at"],
        rejection_flag=bool(row["rejection_flag"]),
        follow_up_sent=bool(row["follow_up_sent"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def recruiter_from_row(row: dict[str, Any]) -> RecruiterRecord:
    return RecruiterRecord(
        id=row["id"],
        recruiter_handle=row["recruiter_handle"],
        status=row["status"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        last_outbound_at=row["last_outbound_at"],
        last_inbound_at=row["last_inbound_at"],
        outbound_count=int(row["outbound_count"]),
        inbound_count=int(row["inbound_count"]),
        positive_reply_count=int(row["positive_reply_count"]),
        negative_reply_count=int(row["negative_reply_count"]),
        notes=row["notes"],
        updated_at=row["updated_at"],
    )


def ensure_recruiter(connection: Any, recruiter_handle: str) -> str:
    row = connection.execute(
        "SELECT * FROM recruiters WHERE recruiter_handle = ?",
        (recruiter_handle,),
    ).fetchone()
    if row is not None:
        now = utc_now()
        connection.execute(
            "UPDATE recruiters SET last_seen_at = ?, updated_at = ? WHERE id = ?",
            (now, now, row["id"]),
        )
        return str(row["id"])
    recruiter_id = str(uuid.uuid4())
    now = utc_now()
    connection.execute(
        """
        INSERT INTO recruiters (
            id, recruiter_handle, status, first_seen_at, last_seen_at,
            last_outbound_at, last_inbound_at, outbound_count, inbound_count,
            positive_reply_count, negative_reply_count, notes, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, NULL, ?)
        """,
        (recruiter_id, recruiter_handle, "new", now, now, None, None, now),
    )
    return recruiter_id


def update_recruiter_outbound(connection: Any, recruiter_handle: str) -> None:
    ensure_recruiter(connection, recruiter_handle)
    now = utc_now()
    connection.execute(
        """
        UPDATE recruiters
        SET status = 'contacted',
            last_seen_at = ?,
            last_outbound_at = ?,
            outbound_count = outbound_count + 1,
            updated_at = ?
        WHERE recruiter_handle = ?
        """,
        (now, now, now, recruiter_handle),
    )


def update_recruiter_inbound(connection: Any, recruiter_handle: str, classification: str) -> None:
    ensure_recruiter(connection, recruiter_handle)
    now = utc_now()
    positive_inc = 1 if classification == "positive" else 0
    negative_inc = 1 if classification == "negative" else 0
    status = "replied_positive" if classification == "positive" else ("replied_negative" if classification == "negative" else "replied")
    connection.execute(
        """
        UPDATE recruiters
        SET status = ?,
            last_seen_at = ?,
            last_inbound_at = ?,
            inbound_count = inbound_count + 1,
            positive_reply_count = positive_reply_count + ?,
            negative_reply_count = negative_reply_count + ?,
            updated_at = ?
        WHERE recruiter_handle = ?
        """,
        (status, now, now, positive_inc, negative_inc, now, recruiter_handle),
    )


def fetch_recruiter_profile(connection: Any, recruiter_handle: str | None) -> dict[str, Any] | None:
    if not recruiter_handle:
        return None
    row = connection.execute(
        "SELECT * FROM recruiters WHERE recruiter_handle = ?",
        (recruiter_handle,),
    ).fetchone()
    if row is None:
        return None
    return {
        "status": row["status"],
        "outbound_count": int(row["outbound_count"]),
        "inbound_count": int(row["inbound_count"]),
        "positive_reply_count": int(row["positive_reply_count"]),
        "negative_reply_count": int(row["negative_reply_count"]),
        "last_outbound_at": row["last_outbound_at"],
        "last_inbound_at": row["last_inbound_at"],
        "notes": row["notes"],
    }


def fetch_latest_conversation_summary(connection: Any, conversation_id: str) -> str | None:
    row = connection.execute(
        """
        SELECT summary_text
        FROM conversation_summaries
        WHERE conversation_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()
    return None if row is None else str(row["summary_text"])


def store_memory_document(
    connection: Any,
    *,
    memory_type: str,
    entity_id: str,
    content_text: str,
    metadata: dict[str, Any],
) -> str:
    memory_id = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO memory_documents (id, memory_type, entity_id, content_text, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (memory_id, memory_type, entity_id, content_text, json.dumps(metadata), utc_now()),
    )
    return memory_id


def fetch_recent_memory_documents(
    connection: Any,
    *,
    memory_type: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    return connection.execute(
        """
        SELECT * FROM memory_documents
        WHERE memory_type = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (memory_type, limit),
    ).fetchall()


def promote_approved_outreach_snippet(
    connection: Any,
    *,
    vacancy_id: str,
    recruiter_handle: str | None,
    structured: dict[str, Any],
    approved_draft: str,
) -> str:
    snippet_text = approved_draft.strip()[:500]
    return store_memory_document(
        connection,
        memory_type="approved_outreach_snippet",
        entity_id=vacancy_id,
        content_text=snippet_text,
        metadata={
            "recruiter_handle": recruiter_handle,
            "title": structured.get("title"),
            "company": structured.get("company"),
            "role_family": structured.get("detected_roles", []),
            "skills": structured.get("skills", []),
        },
    )


def job_from_row(row: dict[str, Any]) -> JobRecord:
    return JobRecord(
        id=row["id"],
        job_type=row["job_type"],
        entity_id=row["entity_id"],
        status=row["status"],
        run_at=row["run_at"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        payload=json.loads(row["payload_json"] or "{}"),
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def ensure_conversation(connection: Any, recruiter_handle: str) -> str:
    ensure_recruiter(connection, recruiter_handle)
    row = connection.execute(
        "SELECT * FROM conversations WHERE recruiter_handle = ?",
        (recruiter_handle,),
    ).fetchone()
    if row is not None:
        return str(row["id"])
    conversation_id = str(uuid.uuid4())
    now = utc_now()
    connection.execute(
        """
        INSERT INTO conversations (
            id, recruiter_handle, status, last_outbound_at, last_inbound_at,
            rejection_flag, follow_up_sent, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (conversation_id, recruiter_handle, "open", None, None, 0, 0, now, now),
    )
    return conversation_id


def schedule_job(
    connection: Any,
    *,
    job_type: str,
    entity_id: str,
    run_at: str,
    payload: dict[str, Any],
    max_attempts: int = 3,
) -> str:
    dedupe_row = connection.execute(
        """
        SELECT id FROM jobs
        WHERE job_type = ? AND entity_id = ? AND status IN ('pending', 'leased')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (job_type, entity_id),
    ).fetchone()
    if dedupe_row is not None:
        return str(dedupe_row["id"])

    job_id = str(uuid.uuid4())
    now = utc_now()
    connection.execute(
        """
        INSERT INTO jobs (
            id, job_type, entity_id, status, run_at, attempts, max_attempts,
            payload_json, last_error, lease_owner, lease_expires_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, NULL, NULL, NULL, ?, ?)
        """,
        (job_id, job_type, entity_id, "pending", run_at, max_attempts, json.dumps(payload), now, now),
    )
    return job_id


def cancel_pending_jobs(connection: Any, *, entity_id: str, job_type: str) -> int:
    updated_at = utc_now()
    cursor = connection.execute(
        """
        UPDATE jobs
        SET status = 'cancelled', updated_at = ?
        WHERE entity_id = ? AND job_type = ? AND status IN ('pending', 'leased')
        """,
        (updated_at, entity_id, job_type),
    )
    return int(cursor.rowcount or 0)


def upsert_periodic_job(
    connection: Any,
    *,
    job_type: str,
    entity_id: str,
    interval_seconds: int,
    payload: dict[str, Any],
    max_attempts: int = 3,
) -> str:
    row = connection.execute(
        "SELECT id FROM jobs WHERE job_type = ? AND entity_id = ? ORDER BY created_at DESC LIMIT 1",
        (job_type, entity_id),
    ).fetchone()
    run_at = compute_expiry(interval_seconds)
    now = utc_now()
    if row is None:
        job_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO jobs (
                id, job_type, entity_id, status, run_at, attempts, max_attempts,
                payload_json, last_error, lease_owner, lease_expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, NULL, NULL, NULL, ?, ?)
            """,
            (job_id, job_type, entity_id, "pending", run_at, max_attempts, json.dumps(payload), now, now),
        )
        return job_id

    job_id = str(row["id"])
    connection.execute(
        """
        UPDATE jobs
        SET status = 'pending',
            run_at = ?,
            payload_json = ?,
            last_error = NULL,
            lease_owner = NULL,
            lease_expires_at = NULL,
            updated_at = ?
        WHERE id = ?
        """,
        (run_at, json.dumps(payload), now, job_id),
    )
    return job_id


def classify_reply(message_text: str) -> str:
    lowered = message_text.lower()
    negative_markers = ("not interested", "no thanks", "stop", "don't write", "do not write", "decline", "no,")
    positive_markers = ("interested", "let's talk", "lets talk", "send cv", "share cv", "sounds good", "yes")
    if any(marker in lowered for marker in negative_markers):
        return "negative"
    if any(marker in lowered for marker in positive_markers):
        return "positive"
    return "needs_manual_reply"


def ingest_recruiter_reply_internal(
    connection: Any,
    *,
    recruiter_handle: str,
    message_text: str,
    source: str,
) -> ConversationReplyResult:
    normalized_handle = normalize_handle(recruiter_handle)
    if not normalized_handle:
        raise HTTPException(status_code=400, detail="Invalid recruiter_handle")

    row = connection.execute(
        "SELECT * FROM conversations WHERE recruiter_handle = ?",
        (normalized_handle,),
    ).fetchone()
    if row is None:
        conversation_id = ensure_conversation(connection, normalized_handle)
        row = connection.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()

    classification = classify_reply(message_text)
    updated_at = utc_now()
    rejection_flag = 1 if classification == "negative" else int(row["rejection_flag"])
    status = "rejected" if classification == "negative" else ("responded" if classification == "positive" else "manual_reply")
    connection.execute(
        """
        UPDATE conversations
        SET status = ?, last_inbound_at = ?, rejection_flag = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, updated_at, rejection_flag, updated_at, row["id"]),
    )
    update_recruiter_inbound(connection, normalized_handle, classification)
    cancelled_jobs = cancel_pending_jobs(connection, entity_id=row["id"], job_type="follow_up_due")
    log_audit(
        connection,
        entity_type="conversation",
        entity_id=row["id"],
        event_type="incoming_reply_ingested",
        payload={
            "classification": classification,
            "source": source,
            "cancelled_jobs": cancelled_jobs,
        },
    )
    updated_row = connection.execute("SELECT * FROM conversations WHERE id = ?", (row["id"],)).fetchone()
    return ConversationReplyResult(
        conversation=conversation_from_row(updated_row),
        classification=classification,
        cancelled_jobs=cancelled_jobs,
    )


async def refresh_conversation_memory(
    *,
    conversation_id: str,
    recruiter_handle: str,
    latest_message: str,
    latest_classification: str,
    source_event: str,
) -> None:
    connection = get_db()
    previous_summary = fetch_latest_conversation_summary(connection, conversation_id)
    recruiter_profile = fetch_recruiter_profile(connection, recruiter_handle)
    connection.close()
    summary_text, summary_source = await generate_conversation_summary_with_astrixa(
        recruiter_handle=recruiter_handle,
        latest_message=latest_message,
        latest_classification=latest_classification,
        previous_summary=previous_summary,
    )
    connection = get_db()
    connection.execute(
        """
        INSERT INTO conversation_summaries (id, conversation_id, summary_text, source_event, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (str(uuid.uuid4()), conversation_id, summary_text, source_event, utc_now()),
    )
    store_memory_document(
        connection,
        memory_type="conversation_summary",
        entity_id=conversation_id,
        content_text=summary_text,
        metadata={
            "recruiter_handle": recruiter_handle,
            "classification": latest_classification,
            "summary_source": summary_source,
            "source_event": source_event,
        },
    )
    if recruiter_profile:
        store_memory_document(
            connection,
            memory_type="recruiter_profile_snapshot",
            entity_id=recruiter_handle,
            content_text=json.dumps(recruiter_profile, ensure_ascii=False),
            metadata={
                "recruiter_handle": recruiter_handle,
                "source_event": source_event,
            },
        )
    connection.commit()
    connection.close()


async def poll_telegram_replies_internal(per_conversation_limit: int) -> TelegramReplyPollResult:
    connection = get_db()
    conversation_rows = connection.execute(
        "SELECT * FROM conversations WHERE recruiter_handle IS NOT NULL ORDER BY updated_at DESC"
    ).fetchall()
    connection.close()

    processed_conversations = 0
    fetched_messages = 0
    ingested_replies = 0
    skipped_duplicates = 0

    async with TelegramClient(
        StringSession(settings.telegram_session_string),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    ) as client:
        for conversation in conversation_rows:
            recruiter_handle = conversation["recruiter_handle"]
            if not recruiter_handle:
                continue
            processed_conversations += 1
            try:
                entity = await client.get_entity(recruiter_handle)
            except Exception:
                continue
            async for message in client.iter_messages(entity, limit=per_conversation_limit):
                if getattr(message, "out", False):
                    continue
                text = (message.message or "").strip()
                if not text:
                    continue
                fetched_messages += 1
                external_message_id = str(message.id)
                conn = get_db()
                existing = conn.execute(
                    """
                    SELECT id FROM inbound_message_events
                    WHERE recruiter_handle = ? AND source = ? AND external_message_id = ?
                    """,
                    (recruiter_handle, "telegram_poll", external_message_id),
                ).fetchone()
                if existing is not None:
                    conn.close()
                    skipped_duplicates += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO inbound_message_events (
                        id, recruiter_handle, source, external_message_id, raw_text, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), recruiter_handle, "telegram_poll", external_message_id, text, utc_now()),
                )
                result = ingest_recruiter_reply_internal(
                    conn,
                    recruiter_handle=recruiter_handle,
                    message_text=text,
                    source="telegram_poll",
                )
                conn.commit()
                conn.close()
                await refresh_conversation_memory(
                    conversation_id=result.conversation.id,
                    recruiter_handle=result.conversation.recruiter_handle,
                    latest_message=text,
                    latest_classification=result.classification,
                    source_event="telegram_poll",
                )
                ingested_replies += 1

    return TelegramReplyPollResult(
        processed_conversations=processed_conversations,
        fetched_messages=fetched_messages,
        ingested_replies=ingested_replies,
        skipped_duplicates=skipped_duplicates,
    )


def split_vacancy_post(raw_text: str) -> list[str]:
    text = raw_text.strip()
    if not text:
        return []

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    bullet_candidates = [
        line for line in lines
        if line.startswith(("—", "-", "•", "*")) and len(line) > 20
    ]
    if len(bullet_candidates) < 2:
        return [text]

    intro_lines: list[str] = []
    items: list[str] = []
    for line in lines:
        if line.startswith(("—", "-", "•", "*")) and len(line) > 20:
            cleaned = re.sub(r"^[—\-•*]\s*", "", line).strip()
            if intro_lines:
                item_text = "\n".join(intro_lines[:2] + [cleaned])
            else:
                item_text = cleaned
            items.append(item_text)
        elif not items:
            intro_lines.append(line)

    if len(items) >= 2:
        return items
    return [text]


def normalize_split_item_title(raw_text: str, fallback_title: str) -> str:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        return fallback_title

    candidate = lines[-1]
    candidate = re.sub(r"^[—\-•*]\s*", "", candidate).strip(" .,-:")
    if not candidate:
        return fallback_title

    lowered = candidate.lower()
    intro_markers = (
        "weekly ",
        "digest",
        "vacancies",
        "jobs",
        "opening",
        "openings",
        "roles",
        "hiring",
    )
    if len(lines) > 1 and any(marker in lowered for marker in intro_markers):
        candidate = lines[-1].strip(" .,-:")

    if len(candidate) > 120:
        candidate = candidate[:117].rstrip() + "..."
    return candidate or fallback_title


def simplify_title(title: str) -> str:
    simplified = re.split(
        r"\.\s*(?:remote|office|hybrid|salary|stack|contact|location)\b",
        title,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" .,-:")
    if len(simplified) > 120:
        simplified = simplified[:117].rstrip() + "..."
    return simplified or title


def normalize_handle(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    cleaned = re.sub(r"^https?://t\.me/", "@", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^t\.me/", "@", cleaned, flags=re.IGNORECASE)
    if not cleaned.startswith("@"):
        cleaned = f"@{cleaned.lstrip('@')}"
    if re.fullmatch(r"@[A-Za-z0-9_]{4,64}", cleaned):
        return cleaned
    return None


SKILL_ALIASES: dict[str, tuple[str, ...]] = {
    "python": ("python",),
    "fastapi": ("fastapi",),
    "backend": ("backend", "back-end", "api"),
    "mlops": ("mlops", "ml ops", "mlops"),
    "llm": ("llm", "llms", "gpt", "rag", "prompting"),
    "docker": ("docker",),
    "postgresql": ("postgresql", "postgres", "psql"),
    "observability": ("observability", "monitoring", "tracing", "metrics", "prometheus", "grafana"),
    "etl": ("etl", "airflow", "dbt", "data pipeline", "pipelines"),
}

LOCATION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("berlin", ("berlin",)),
    ("tbilisi", ("tbilisi", "tbilisi, georgia")),
    ("georgia", ("georgia", "georgia country")),
    ("europe", ("europe", "eu", "european union", "emea")),
    ("london", ("london", "uk", "united kingdom")),
    ("warsaw", ("warsaw", "poland")),
    ("amsterdam", ("amsterdam", "netherlands")),
    ("hybrid", ("hybrid", "гибрид", "hybrid mode")),
    ("remote", ("remote", "remotely", "удален", "удаленно", "удалённо", "work from anywhere")),
)

ROLE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("backend engineer", ("backend engineer", "backend developer", "python backend", "python engineer", "api engineer")),
    ("python engineer", ("python engineer", "python developer")),
    ("ml engineer", ("ml engineer", "machine learning engineer")),
    ("mlops engineer", ("mlops engineer", "ml ops engineer", "machine learning ops")),
    ("platform engineer", ("platform engineer", "platform developer", "infra engineer", "infrastructure engineer")),
    ("data engineer", ("data engineer", "etl engineer", "analytics engineer")),
    ("frontend engineer", ("frontend engineer", "frontend developer")),
    ("designer", ("designer", "product designer", "ux designer")),
    ("qa engineer", ("qa engineer", "test engineer", "sdet")),
    ("ios engineer", ("ios engineer", "ios developer")),
    ("android engineer", ("android engineer", "android developer")),
)

CURRENCY_ALIASES: dict[str, str] = {
    "$": "USD",
    "usd": "USD",
    "usdt": "USD",
    "eur": "EUR",
    "€": "EUR",
    "gel": "GEL",
    "₾": "GEL",
    "₽": "RUB",
    "rub": "RUB",
}


def extract_company(raw_text: str, title: str, primary_line: str) -> str | None:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    search_space = [title, primary_line] + lines[:3]
    patterns = (
        r"\b(?:at|for)\s+([A-Z][A-Za-z0-9&._-]*(?:\s+[A-Z][A-Za-z0-9&._-]*){0,4})",
        r"\bcompany[:\s]+([A-Z][A-Za-z0-9&._-]*(?:\s+[A-Z][A-Za-z0-9&._-]*){0,4})",
        r"\bjoin\s+([A-Z][A-Za-z0-9&._-]*(?:\s+[A-Z][A-Za-z0-9&._-]*){0,4})",
    )
    stop_markers = r"\b(remote|salary|office|hybrid|location|team|format|role|contact|telegram|dm|skills?)\b"

    for chunk in search_space:
        for pattern in patterns:
            match = re.search(pattern, chunk)
            if not match:
                continue
            company = match.group(1).strip(" .,-:")
            company = re.split(stop_markers, company, maxsplit=1, flags=re.IGNORECASE)[0].strip(" .,-:")
            if company and company.lower() not in {"python", "backend", "ml", "engineer", "developer"}:
                return company

    if " at " in title.lower():
        tail = re.split(r"\bat\b", title, maxsplit=1, flags=re.IGNORECASE)[1].strip(" .,-:")
        tail = re.split(stop_markers, tail, maxsplit=1, flags=re.IGNORECASE)[0].strip(" .,-:")
        if tail:
            return tail
    return None


def extract_location(raw_text: str, remote: bool) -> str | None:
    lowered = raw_text.lower()
    for normalized, patterns in LOCATION_PATTERNS:
        if any(pattern in lowered for pattern in patterns):
            return normalized
    return "remote" if remote else None


def extract_skills(raw_text: str) -> list[str]:
    lowered = raw_text.lower()
    words = normalize_words(raw_text)
    detected: list[str] = []
    for canonical, aliases in SKILL_ALIASES.items():
        if any(alias in lowered or alias in words for alias in aliases):
            detected.append(canonical)
    return sorted(set(detected))


def normalize_salary_number(raw_value: str) -> int:
    cleaned = raw_value.lower().replace(",", "").replace(" ", "")
    multiplier = 1
    if cleaned.endswith("k"):
        multiplier = 1000
        cleaned = cleaned[:-1]
    return int(float(cleaned) * multiplier)


def extract_salary(raw_text: str) -> tuple[int | None, int | None, str | None, str | None]:
    lowered = raw_text.lower()
    salary_type = None
    if any(marker in lowered for marker in ("gross", "before tax", "до вычета", "грязными")):
        salary_type = "gross"
    elif any(marker in lowered for marker in ("net", "after tax", "на руки", "чистыми")):
        salary_type = "net"

    patterns = (
        r"(?P<min>\d+(?:[.,]\d+)?k?)(?:\s*[-–]\s*(?P<max>\d+(?:[.,]\d+)?k?))?\s*(?P<currency>\$|€|usd|eur|gel|₾|₽|rub)\b",
        r"(?P<currency>\$|€|usd|eur|gel|₾|₽|rub)\s*(?P<min>\d+(?:[.,]\d+)?k?)(?:\s*[-–]\s*(?P<max>\d+(?:[.,]\d+)?k?))?\b",
    )

    for pattern in patterns:
        match = re.search(pattern, raw_text, flags=re.IGNORECASE)
        if not match:
            continue
        currency = CURRENCY_ALIASES.get(match.group("currency").lower())
        salary_min = normalize_salary_number(match.group("min"))
        salary_max = normalize_salary_number(match.group("max")) if match.groupdict().get("max") else None
        return salary_min, salary_max, currency, salary_type

    return None, None, None, salary_type


def detect_roles(raw_text: str) -> list[str]:
    lowered = raw_text.lower()
    detected: list[str] = []
    for canonical, aliases in ROLE_PATTERNS:
        if any(alias in lowered for alias in aliases):
            detected.append(canonical)
    return sorted(set(detected))


def extract_title(raw_text: str) -> str:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        return "Untitled vacancy"

    for line in lines[:3]:
        cleaned = re.sub(r"^[—\-•*]\s*", "", line).strip()
        if len(cleaned) < 8:
            continue
        lowered = cleaned.lower()
        if any(marker in lowered for marker in ("salary", "contact:", "telegram", "stack:", "company:")):
            continue
        return cleaned[:120]
    return lines[0][:120]


def extract_recruiter_handle(raw_text: str, explicit_handle: str | None) -> tuple[str | None, str]:
    normalized_explicit = normalize_handle(explicit_handle)
    if normalized_explicit:
        return normalized_explicit, "explicit_input"

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    contact_lines = [
        line for line in lines
        if any(marker in line.lower() for marker in ("contact", "dm", "telegram", "tg:", "reach", "write"))
    ]
    search_space = contact_lines + lines[-2:]

    for chunk in search_space:
        match = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,64})", chunk, flags=re.IGNORECASE)
        if match:
            return normalize_handle(match.group(1)), "extracted_tme_link"
        match = re.search(r"(?<![A-Za-z0-9._%+\-])@([A-Za-z0-9_]{4,64})\b", chunk)
        if match:
            return normalize_handle(match.group(1)), "extracted_at_handle"

    if contact_lines:
        return None, "contact_line_without_handle"
    return None, "no_contact_handle_found"


def extract_contact_email(raw_text: str) -> tuple[str | None, str]:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    contact_lines = [
        line for line in lines
        if any(marker in line.lower() for marker in ("contact", "email", "mail", "reach", "write"))
    ]
    search_space = contact_lines + lines[-3:]
    for chunk in search_space:
        match = re.search(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", chunk, flags=re.IGNORECASE)
        if match:
            return match.group(0).lower(), "extracted_email"
    if contact_lines:
        return None, "contact_line_without_email"
    return None, "no_contact_email_found"


def resolve_contact_channel(contact_email: str | None, recruiter_handle: str | None) -> tuple[str | None, str | None]:
    if contact_email:
        return "email", contact_email
    if recruiter_handle:
        return "telegram", recruiter_handle
    return None, None


def parse_vacancy(raw_text: str, recruiter_handle: str | None) -> dict[str, Any]:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    title = extract_title(raw_text)
    primary_line = lines[-1] if lines else raw_text
    words = normalize_words(raw_text)
    seniority = (
        "senior" if any(token in words for token in ("senior", "lead", "staff", "principal"))
        else "middle" if any(token in words for token in ("middle", "mid", "regular"))
        else "junior" if any(token in words for token in ("junior", "jun"))
        else "unknown"
    )
    raw_lower = raw_text.lower()
    remote = (
        "remote" in words
        or "удален" in raw_lower
        or "удаленно" in raw_lower
        or "удалённо" in raw_lower
        or "work from anywhere" in raw_lower
    )
    salary_min, salary_max, currency, salary_type = extract_salary(raw_text)

    company = extract_company(raw_text, title, primary_line)
    location = extract_location(raw_text, remote)
    detected_roles = detect_roles(raw_text)
    extracted_skills = extract_skills(raw_text)
    title = simplify_title(normalize_split_item_title(raw_text, title))
    extracted_handle, contact_extraction_reason = extract_recruiter_handle(raw_text, recruiter_handle)
    contact_email, email_extraction_reason = extract_contact_email(raw_text)
    preferred_contact_channel, contact_target = resolve_contact_channel(contact_email, extracted_handle)
    allowed_locations = [str(value).lower() for value in CANDIDATE_PREFERENCES.get("allowed_locations", [])]
    location_allowed = True if not location or not allowed_locations else location in allowed_locations
    work_mode = "hybrid" if "hybrid" in raw_lower else ("remote" if remote else "onsite")
    return {
        "title": title,
        "company": company,
        "seniority": seniority,
        "remote": remote,
        "work_mode": work_mode,
        "location": location,
        "location_allowed": location_allowed,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "currency": currency,
        "salary_type": salary_type,
        "detected_roles": detected_roles,
        "skills": extracted_skills,
        "recruiter_handle": extracted_handle,
        "contact_email": contact_email,
        "preferred_contact_channel": preferred_contact_channel,
        "contact_target": contact_target,
        "contact_extraction_reason": contact_extraction_reason,
        "email_extraction_reason": email_extraction_reason,
        "contact_extraction_status": "resolved" if (extracted_handle or contact_email) else "missing",
        "email_extraction_status": "resolved" if contact_email else "missing",
        "preview": raw_text[:500],
    }


def explain_vacancy(structured: dict[str, Any], raw_text: str) -> tuple[float, list[str], dict[str, Any], str, list[str]]:
    vacancy_words = normalize_words(raw_text)
    candidate_skills = [str(skill).lower() for skill in CANDIDATE_PROFILE.get("skills", [])]
    matched_skills = sorted(skill for skill in candidate_skills if skill in vacancy_words)
    preferred_roles = [str(role).lower() for role in CANDIDATE_PREFERENCES.get("preferred_roles", [])]
    excluded_roles = [str(role).lower() for role in CANDIDATE_PREFERENCES.get("excluded_roles", [])]
    detected_roles = [str(role).lower() for role in structured.get("detected_roles", [])]
    title_lower = structured["title"].lower()

    role_match = any(role in title_lower or role in detected_roles for role in preferred_roles)
    excluded_role_hit = any(role in title_lower or role in detected_roles for role in excluded_roles)
    allowed_locations = [str(value).lower() for value in CANDIDATE_PREFERENCES.get("allowed_locations", [])]
    work_mode = str(structured.get("work_mode") or "").lower()
    remote_required = bool(CANDIDATE_PREFERENCES.get("remote_only", False))
    is_remote_compatible = (not remote_required) or work_mode == "remote"
    location_value = str(structured.get("location") or "").lower()
    location_match = True if not allowed_locations or not location_value else location_value in allowed_locations
    remote_match = is_remote_compatible and location_match

    salary_min = structured.get("salary_min")
    salary_currency = structured.get("currency")
    preferred_currency = str(CANDIDATE_PREFERENCES.get("currency", "USD")).upper()
    salary_match = True
    salary_reason = "salary_missing"
    if salary_min is not None and salary_currency:
        salary_match = salary_currency == preferred_currency and salary_min >= int(CANDIDATE_PREFERENCES.get("min_salary", 0))
        salary_reason = "salary_match" if salary_match else "salary_below_expectation_or_currency_mismatch"

    excluded_keywords = [str(item).lower() for item in CANDIDATE_PREFERENCES.get("excluded_keywords", [])]
    keyword_block = next((keyword for keyword in excluded_keywords if keyword in raw_text.lower()), None)

    must_have_skills = [str(skill).lower() for skill in CANDIDATE_PREFERENCES.get("must_have_skills", [])]
    missing_must_have = [skill for skill in must_have_skills if skill not in vacancy_words]

    skills_score = len(matched_skills) / max(1, len(candidate_skills))
    role_score = 1.0 if role_match else 0.0
    seniority_score = 1.0 if structured.get("seniority") in {CANDIDATE_PROFILE.get("seniority"), "unknown"} else 0.3
    remote_score = 1.0 if remote_match else 0.0
    salary_score = 0.7 if salary_match and salary_reason == "salary_missing" else (1.0 if salary_match else 0.0)

    score = round((skills_score * 0.45) + (role_score * 0.2) + (seniority_score * 0.1) + (remote_score * 0.15) + (salary_score * 0.1), 4)
    breakdown = {
        "skills_score": round(skills_score, 4),
        "role_score": round(role_score, 4),
        "seniority_score": round(seniority_score, 4),
        "remote_score": round(remote_score, 4),
        "salary_score": round(salary_score, 4),
        "matched_skills": matched_skills,
        "missing_must_have_skills": missing_must_have,
        "salary_reason": salary_reason,
        "work_mode": work_mode,
        "location_match": location_match,
    }

    filter_reasons: list[str] = []
    if excluded_role_hit:
        filter_reasons.append("excluded_role")
    if not is_remote_compatible:
        filter_reasons.append("remote_required")
    if not location_match:
        filter_reasons.append("location_not_allowed")
    if keyword_block:
        filter_reasons.append(f"excluded_keyword:{keyword_block}")
    if missing_must_have:
        filter_reasons.append("missing_must_have_skills")
    if salary_reason == "salary_below_expectation_or_currency_mismatch":
        filter_reasons.append("salary_below_expectation_or_currency_mismatch")
    if not role_match:
        filter_reasons.append("role_not_preferred")

    filter_decision = "allow"
    if filter_reasons:
        hard_failures = {
            "excluded_role",
            "remote_required",
            "location_not_allowed",
            "missing_must_have_skills",
            "salary_below_expectation_or_currency_mismatch",
        }
        if any(reason in hard_failures for reason in filter_reasons):
            filter_decision = "deny"
        else:
            filter_decision = "manual_review"

    return score, matched_skills, breakdown, filter_decision, filter_reasons


def build_context_bundle(
    *,
    structured: dict[str, Any],
    raw_text: str,
    matched_skills: list[str],
    filter_decision: str,
    filter_reasons: list[str],
    recruiter_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    snippets: list[str] = []

    profile_summary = str(CANDIDATE_PROFILE.get("summary") or settings.user_headline).strip()
    if profile_summary:
        sources.append({"source": "candidate_profile.summary", "kind": "profile_summary"})
        snippets.append(profile_summary)

    candidate_skills = [str(skill) for skill in CANDIDATE_PROFILE.get("skills", settings.user_skills)]
    if candidate_skills:
        skills_snippet = "Skills: " + ", ".join(candidate_skills[:12])
        sources.append({"source": "candidate_profile.skills", "kind": "skills"})
        snippets.append(skills_snippet)

    preferred_roles = [str(role) for role in CANDIDATE_PREFERENCES.get("preferred_roles", [])]
    if preferred_roles:
        preferences_snippet = "Preferred roles: " + ", ".join(preferred_roles[:8])
        sources.append({"source": "candidate_preferences.preferred_roles", "kind": "preferences"})
        snippets.append(preferences_snippet)

    policy_constraints = {
        "filter_decision": filter_decision,
        "filter_reasons": filter_reasons,
        "remote_only": CANDIDATE_PREFERENCES.get("remote_only", False),
        "min_salary": CANDIDATE_PREFERENCES.get("min_salary"),
        "currency": CANDIDATE_PREFERENCES.get("currency"),
    }
    sources.append({"source": "policy.constraints", "kind": "policy"})
    snippets.append(f"Policy constraints: {json.dumps(policy_constraints, ensure_ascii=False)}")

    vacancy_summary = {
        "title": structured.get("title"),
        "company": structured.get("company"),
        "location": structured.get("location"),
        "recruiter_handle": structured.get("recruiter_handle"),
        "contact_email": structured.get("contact_email"),
        "preferred_contact_channel": structured.get("preferred_contact_channel"),
        "matched_skills": matched_skills,
    }
    sources.append({"source": "vacancy.structured", "kind": "vacancy"})
    snippets.append(f"Vacancy summary: {json.dumps(vacancy_summary, ensure_ascii=False)}")

    recruiter_handle = structured.get("recruiter_handle")
    if recruiter_handle:
        resolved_recruiter_profile = recruiter_profile
        if resolved_recruiter_profile is None:
            connection = get_db()
            resolved_recruiter_profile = fetch_recruiter_profile(connection, recruiter_handle)
            connection.close()
        if resolved_recruiter_profile:
            sources.append({"source": "recruiter.profile", "kind": "recruiter_profile"})
            snippets.append(
                f"Recruiter profile: handle={recruiter_handle} data={json.dumps(resolved_recruiter_profile, ensure_ascii=False)}"
            )

    raw_excerpt = raw_text[:600]
    sources.append({"source": "vacancy.raw_excerpt", "kind": "raw_text"})
    snippets.append(raw_excerpt)

    conversation_id = structured.get("conversation_id")
    if conversation_id:
        connection = get_db()
        latest_summary = fetch_latest_conversation_summary(connection, conversation_id)
        recent_snippets = fetch_recent_memory_documents(connection, memory_type="approved_outreach_snippet", limit=3)
        connection.close()
        if latest_summary:
            sources.append({"source": "conversation.latest_summary", "kind": "conversation_summary"})
            snippets.append(f"Conversation summary: {latest_summary}")
        for index, snippet_row in enumerate(recent_snippets, start=1):
            snippet_text = str(snippet_row["content_text"]).strip()
            if not snippet_text:
                continue
            snippet_meta = json.loads(snippet_row["metadata_json"] or "{}")
            sources.append({"source": f"memory.approved_outreach_snippet.{index}", "kind": "approved_snippet"})
            snippets.append(
                f"Approved outreach snippet {index}: {snippet_text} | meta={json.dumps(snippet_meta, ensure_ascii=False)}"
            )
    else:
        connection = get_db()
        recent_snippets = fetch_recent_memory_documents(connection, memory_type="approved_outreach_snippet", limit=3)
        connection.close()
        for index, snippet_row in enumerate(recent_snippets, start=1):
            snippet_text = str(snippet_row["content_text"]).strip()
            if not snippet_text:
                continue
            snippet_meta = json.loads(snippet_row["metadata_json"] or "{}")
            sources.append({"source": f"memory.approved_outreach_snippet.{index}", "kind": "approved_snippet"})
            snippets.append(
                f"Approved outreach snippet {index}: {snippet_text} | meta={json.dumps(snippet_meta, ensure_ascii=False)}"
            )

    truncation_decisions: list[str] = []
    token_budget = settings.context_budget_tokens
    included_snippets: list[str] = []
    estimated_tokens = 0
    for index, snippet in enumerate(snippets):
        snippet_tokens = estimate_tokens(snippet)
        if estimated_tokens + snippet_tokens > token_budget:
            truncation_decisions.append(f"drop_source:{sources[index]['source']}")
            continue
        included_snippets.append(snippet)
        estimated_tokens += snippet_tokens

    return {
        "task_type": "draft_generation",
        "included_sources": sources[: len(included_snippets)],
        "estimated_token_usage": estimated_tokens,
        "token_budget": token_budget,
        "truncation_decisions": truncation_decisions,
        "snippets": included_snippets,
    }


def run_ingestion_agent(raw_text: str, recruiter_handle: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    structured = parse_vacancy(raw_text, recruiter_handle)
    trace = {
        "agent": "ingestion_agent",
        "status": "completed",
        "contact_extraction_status": structured.get("contact_extraction_status"),
        "contact_extraction_reason": structured.get("contact_extraction_reason"),
    }
    AGENT_EXECUTIONS.labels(agent="ingestion_agent", outcome="completed").inc()
    return structured, trace


def run_matching_decision_agent(structured: dict[str, Any], raw_text: str) -> tuple[float, list[str], dict[str, Any], str, list[str], dict[str, Any]]:
    score, matched_skills, score_breakdown, filter_decision, filter_reasons = explain_vacancy(structured, raw_text)
    trace = {
        "agent": "matching_decision_agent",
        "status": "completed",
        "score": score,
        "filter_decision": filter_decision,
        "filter_reasons": filter_reasons,
    }
    AGENT_EXECUTIONS.labels(agent="matching_decision_agent", outcome="completed").inc()
    return score, matched_skills, score_breakdown, filter_decision, filter_reasons, trace


def fallback_draft(structured: dict[str, Any], matched_skills: list[str]) -> str:
    skills = ", ".join(matched_skills[:4]) if matched_skills else "backend and infrastructure work"
    preferred_channel = structured.get("preferred_contact_channel") or "telegram"
    recruiter = structured.get("recruiter_handle") if preferred_channel == "telegram" else None
    greeting = f"Hello {recruiter}." if recruiter else "Hello."
    return (
        f"{greeting} I saw the vacancy '{structured['title']}' and it looks relevant to my background. "
        f"My experience is strongest in {skills}. If the role is still актуальна, I can share a concise profile "
        "and examples of relevant backend/LLM infrastructure work."
    )


def sanitize_draft_text(raw_text: str, recruiter_handle: str | None, source_text: str) -> str:
    text = raw_text.replace("\r", "\n").strip()
    text = re.sub(r"\[(.*?)\]", "", text)
    text = re.sub(r"\{(.*?)\}", "", text)
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    filtered_lines: list[str] = []
    banned_exact = {
        "best regards,",
        "best regards",
        "regards,",
        "regards",
        "sincerely,",
        "sincerely",
        "kind regards,",
        "kind regards",
    }
    banned_contains = (
        "your name",
        "position name",
        "company name",
        "recruiter's name",
        "recruiter name",
    )

    for line in lines:
        lowered = line.lower()
        if lowered in banned_exact:
            continue
        if any(fragment in lowered for fragment in banned_contains):
            continue
        filtered_lines.append(line)

    text = " ".join(filtered_lines)
    text = re.sub(r"\s+", " ", text).strip(" .,;:-")

    source_lower = source_text.lower()
    if "year" not in source_lower and "лет" not in source_lower:
        text = re.sub(r"\b\d+\+?\s+years?\b", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\b\d+\+?\s+yrs?\b", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip(" .,;:-")

    text = text[:500].strip()

    if not text:
        return ""

    if recruiter_handle:
        handle_pattern = re.escape(recruiter_handle)
        text = re.sub(rf"^{handle_pattern}\s+hi,?\s*", f"Hi {recruiter_handle}, ", text, flags=re.IGNORECASE)
        text = re.sub(rf"^{handle_pattern}\s+hello,?\s*", f"Hello {recruiter_handle}, ", text, flags=re.IGNORECASE)
        if recruiter_handle.lower() not in text.lower():
            text = re.sub(r"^hi\s+\w+,?\s*", f"Hi {recruiter_handle}, ", text, flags=re.IGNORECASE)
            text = re.sub(r"^hello\s+\w+,?\s*", f"Hello {recruiter_handle}, ", text, flags=re.IGNORECASE)

    if not re.search(r"[.!?]$", text):
        text += "."

    return text


def contact_greeting_handle(structured: dict[str, Any]) -> str | None:
    if structured.get("preferred_contact_channel") == "email":
        return None
    return structured.get("recruiter_handle")


async def generate_draft_with_astrixa(
    structured: dict[str, Any],
    matched_skills: list[str],
    context_bundle: dict[str, Any],
) -> tuple[str, str]:
    recruiter_hint = structured.get("recruiter_handle") or structured.get("contact_email") or "there"
    preferred_channel = structured.get("preferred_contact_channel") or "telegram"
    context_snippets = "\n".join(str(item) for item in context_bundle.get("snippets", [])[:6])
    style_instruction = (
        "Write like a concise first-contact email, not like Telegram chat."
        if preferred_channel == "email"
        else "Write like a real Telegram outreach message, not like a formal email."
    )
    prompt = (
        "You are drafting a short first-contact outreach message to a recruiter.\n"
        "Keep it under 500 characters, professional, direct, no hype.\n"
        "Use a single compact paragraph.\n"
        "Do not use placeholders, brackets, signatures, sign-offs, markdown, or bullet points.\n"
        "Do not write [Your Name], [Recruiter's Name], [Company], or similar placeholders.\n"
        "Do not invent facts that are not present in the candidate profile or vacancy text.\n"
        "Do not add years of experience, previous employers, or achievements unless explicitly provided.\n"
        f"Preferred contact channel: {preferred_channel}.\n"
        f"If you greet the recruiter, use this exact handle: {recruiter_hint}.\n"
        f"{style_instruction}\n"
        f"Candidate headline: {settings.user_headline}\n"
        f"Candidate skills: {', '.join(CANDIDATE_PROFILE.get('skills', settings.user_skills))}\n"
        f"Candidate summary: {CANDIDATE_PROFILE.get('summary', settings.user_headline)}\n"
        f"Matched skills: {', '.join(matched_skills) if matched_skills else 'none'}\n"
        f"Vacancy title: {structured['title']}\n"
        f"Vacancy company: {structured.get('company') or 'unknown'}\n"
        f"Vacancy preview: {structured['preview']}\n"
        f"Context bundle snippets:\n{context_snippets}\n"
        "Return only the final message text."
    )
    headers = {
        "authorization": f"Bearer {settings.astrixa_token}",
        "content-type": "application/json",
    }
    payload = {
        "model": settings.astrixa_model,
        "messages": [{"role": "user", "content": prompt}],
        "metadata": {
            "project": "tg_outreach_poc",
            "workflow": "draft_generation",
            "anonymization_mode": "off",
            "anonymization_profile": "none",
        },
    }
    try:
        async with httpx.AsyncClient(timeout=settings.astrixa_timeout_seconds) as client:
            response = await client.post(f"{settings.astrixa_base_url}/v1/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        ASTRIXA_CALLS.labels(outcome="error").inc()
        return fallback_draft(structured, matched_skills), f"fallback:http_error:{exc.__class__.__name__}"

    output_text = str(data.get("output_text", "")).strip()
    if not output_text:
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first_choice = choices[0] or {}
            message = first_choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                output_text = content.strip()
    if not output_text:
        ASTRIXA_CALLS.labels(outcome="empty").inc()
        return fallback_draft(structured, matched_skills), "fallback:empty_response"

    sanitized = sanitize_draft_text(output_text, contact_greeting_handle(structured), structured["preview"])
    if not sanitized:
        ASTRIXA_CALLS.labels(outcome="empty").inc()
        return fallback_draft(structured, matched_skills), "fallback:sanitized_empty"

    ASTRIXA_CALLS.labels(outcome="success").inc()
    return sanitized, "astrixa"


async def run_generation_agent(
    structured: dict[str, Any],
    matched_skills: list[str],
    context_bundle: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    draft_text, draft_source = await generate_draft_with_astrixa(structured, matched_skills, context_bundle)
    trace = {
        "agent": "generation_agent",
        "status": "completed",
        "draft_source": draft_source,
        "used_recruiter_handle": structured.get("recruiter_handle"),
        "estimated_token_usage": context_bundle.get("estimated_token_usage"),
        "truncation_decisions": context_bundle.get("truncation_decisions", []),
    }
    outcome = "fallback" if draft_source.startswith("fallback:") else "completed"
    AGENT_EXECUTIONS.labels(agent="generation_agent", outcome=outcome).inc()
    return draft_text, draft_source, trace


async def generate_follow_up_with_astrixa(
    *,
    recruiter_handle: str,
    vacancy_title: str,
    company: str | None,
    previous_draft: str,
    conversation_summary: str | None = None,
) -> tuple[str, str]:
    prompt = (
        "You are drafting a short Telegram follow-up to a recruiter after no reply.\n"
        "Keep it under 350 characters, polite, direct, low-pressure.\n"
        "Do not sound spammy. Do not invent facts.\n"
        f"Recruiter handle: {recruiter_handle}\n"
        f"Vacancy title: {vacancy_title}\n"
        f"Company: {company or 'unknown'}\n"
        f"Conversation summary: {conversation_summary or 'none'}\n"
        f"Previous outreach draft: {previous_draft}\n"
        "Return only the final follow-up message text."
    )
    headers = {
        "authorization": f"Bearer {settings.astrixa_token}",
        "content-type": "application/json",
    }
    payload = {
        "model": settings.astrixa_model,
        "messages": [{"role": "user", "content": prompt}],
        "metadata": {
            "project": "tg_outreach_poc",
            "workflow": "follow_up_generation",
            "anonymization_mode": "off",
            "anonymization_profile": "none",
        },
    }
    try:
        async with httpx.AsyncClient(timeout=settings.astrixa_timeout_seconds) as client:
            response = await client.post(f"{settings.astrixa_base_url}/v1/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        ASTRIXA_CALLS.labels(outcome="error").inc()
        fallback = f"Hi {recruiter_handle}, following up on the {vacancy_title} role{f' at {company}' if company else ''}. If the position is still open, I’d be glad to share more relevant details."
        return fallback[:350], f"fallback:http_error:{exc.__class__.__name__}"

    output_text = str(data.get("output_text", "")).strip()
    if not output_text:
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first_choice = choices[0] or {}
            message = first_choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                output_text = content.strip()
    if not output_text:
        ASTRIXA_CALLS.labels(outcome="empty").inc()
        fallback = f"Hi {recruiter_handle}, following up on the {vacancy_title} role{f' at {company}' if company else ''}. If it is still актуальна, I’d be happy to continue the conversation."
        return fallback[:350], "fallback:empty_response"

    sanitized = sanitize_draft_text(output_text, recruiter_handle, previous_draft)[:350]
    if not sanitized:
        sanitized = f"Hi {recruiter_handle}, following up on the {vacancy_title} role. If relevant, I’d be glad to share more context."
        return sanitized[:350], "fallback:sanitized_empty"
    ASTRIXA_CALLS.labels(outcome="success").inc()
    return sanitized, "astrixa"


async def generate_conversation_summary_with_astrixa(
    *,
    recruiter_handle: str,
    latest_message: str,
    latest_classification: str,
    previous_summary: str | None,
) -> tuple[str, str]:
    prompt = (
        "Summarize a recruiter conversation state for future prompt context.\n"
        "Keep it under 120 words. Mention recruiter intent, current status, and next action.\n"
        "Do not include unnecessary raw wording.\n"
        f"Recruiter handle: {recruiter_handle}\n"
        f"Previous summary: {previous_summary or 'none'}\n"
        f"Latest classification: {latest_classification}\n"
        f"Latest inbound message: {latest_message}\n"
        "Return only the summary text."
    )
    headers = {
        "authorization": f"Bearer {settings.astrixa_token}",
        "content-type": "application/json",
    }
    payload = {
        "model": settings.astrixa_model,
        "messages": [{"role": "user", "content": prompt}],
        "metadata": {
            "project": "tg_outreach_poc",
            "workflow": "conversation_summary",
            "anonymization_mode": "off",
            "anonymization_profile": "none",
        },
    }
    try:
        async with httpx.AsyncClient(timeout=settings.astrixa_timeout_seconds) as client:
            response = await client.post(f"{settings.astrixa_base_url}/v1/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        ASTRIXA_CALLS.labels(outcome="error").inc()
        fallback = f"Recruiter {recruiter_handle} classified as {latest_classification}. Latest message indicates current conversation status should be handled accordingly."
        return fallback, f"fallback:http_error:{exc.__class__.__name__}"

    output_text = str(data.get("output_text", "")).strip()
    if not output_text:
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first_choice = choices[0] or {}
            message = first_choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                output_text = content.strip()
    if not output_text:
        ASTRIXA_CALLS.labels(outcome="empty").inc()
        fallback = f"Recruiter {recruiter_handle} classified as {latest_classification}. Latest message captured for manual review."
        return fallback, "fallback:empty_response"
    ASTRIXA_CALLS.labels(outcome="success").inc()
    return output_text[:800], "astrixa"


def run_execution_safety_agent(
    *,
    connection: Any,
    structured: dict[str, Any],
    filter_decision: str,
    filter_reasons: list[str],
) -> tuple[str, dict[str, Any]]:
    emergency_state = get_emergency_stop_state(connection)
    if emergency_state.enabled:
        AGENT_EXECUTIONS.labels(agent="execution_safety_agent", outcome="blocked").inc()
        return "manual_review", {
            "agent": "execution_safety_agent",
            "status": "blocked",
            "reason": "emergency_stop_enabled",
        }

    status = "allow" if filter_decision == "allow" else "manual_review"
    if not structured.get("contact_target") and filter_decision == "allow":
        status = "manual_review"
        filter_reasons.append("missing_contact_target")

    trace = {
        "agent": "execution_safety_agent",
        "status": "completed",
        "decision": status,
        "has_recruiter_handle": bool(structured.get("recruiter_handle")),
        "has_contact_email": bool(structured.get("contact_email")),
        "preferred_contact_channel": structured.get("preferred_contact_channel"),
    }
    AGENT_EXECUTIONS.labels(agent="execution_safety_agent", outcome="completed").inc()
    return status, trace


async def send_operator_notification(record: VacancyRecord) -> None:
    if not settings.notify_target:
        NOTIFICATION_CALLS.labels(outcome="disabled").inc()
        return
    if not settings.telegram_api_id or not settings.telegram_api_hash or not settings.telegram_session_string:
        NOTIFICATION_CALLS.labels(outcome="not_configured").inc()
        return

    event_type = "awaiting_approval" if record.status == "awaiting_approval" else "manual_review"
    dedupe_key = f"{event_type}:{record.id}:{record.status}"
    summary_lines = [
        f"[{event_type}] {record.title}",
        f"Source: {record.source_channel}",
        f"Score: {record.score} | Decision: {record.filter_decision}",
    ]
    if record.filter_reasons:
        summary_lines.append(f"Reasons: {', '.join(record.filter_reasons)}")
    matched_skills = record.score_breakdown.get("matched_skills") or []
    if matched_skills:
        summary_lines.append(f"Skills: {', '.join(matched_skills[:6])}")
    if record.draft_text:
        summary_lines.append(f"Draft: {record.draft_text[:280]}")
    message = "\n".join(summary_lines)[:3500]
    await send_control_notification(
        event_type=event_type,
        entity_type="vacancy",
        entity_id=record.id,
        dedupe_key=dedupe_key,
        payload={
            "status": record.status,
            "score": record.score,
            "filter_decision": record.filter_decision,
        },
        message=message,
    )


async def send_control_notification(
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    dedupe_key: str,
    payload: dict[str, Any],
    message: str,
) -> None:
    connection = get_db()
    existing = connection.execute(
        "SELECT dedupe_key FROM notification_events WHERE dedupe_key = ?",
        (dedupe_key,),
    ).fetchone()
    if existing is not None:
        connection.close()
        NOTIFICATION_CALLS.labels(outcome="deduped").inc()
        return

    try:
        async with TelegramClient(
            StringSession(settings.telegram_session_string),
            settings.telegram_api_id,
            settings.telegram_api_hash,
        ) as client:
            await client.send_message(settings.notify_target, message)
    except Exception:
        connection.close()
        NOTIFICATION_CALLS.labels(outcome="error").inc()
        return

    connection.execute(
        """
        INSERT INTO notification_events (dedupe_key, entity_type, entity_id, event_type, target, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dedupe_key,
            entity_type,
            entity_id,
            event_type,
            settings.notify_target,
            json.dumps(payload),
            utc_now(),
        ),
    )
    connection.commit()
    connection.close()
    NOTIFICATION_CALLS.labels(outcome="sent").inc()


def vacancy_from_row(row: dict[str, Any]) -> VacancyRecord:
    structured_data = json.loads(row["structured_json"])
    return VacancyRecord(
        id=row["id"],
        source_channel=row["source_channel"],
        recruiter_handle=row["recruiter_handle"],
        contact_email=structured_data.get("contact_email"),
        title=row["title"],
        status=row["status"],
        score=row["score"],
        score_breakdown=json.loads(row["score_breakdown_json"]),
        filter_decision=row["filter_decision"],
        filter_reasons=json.loads(row["filter_reasons_json"]),
        draft_text=row["draft_text"],
        raw_text=row["raw_text"],
        structured_data=structured_data,
        context_bundle=json.loads(row["context_bundle_json"] or "{}"),
        approval_expires_at=row["approval_expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def send_recruiter_message(handle: str, message_text: str) -> None:
    async with TelegramClient(
        StringSession(settings.telegram_session_string),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    ) as client:
        await client.send_message(handle, message_text)


def _send_email_sync(target_email: str, message_text: str, subject: str) -> None:
    if not settings.smtp_host or not settings.smtp_from_email:
        raise RuntimeError("SMTP is not configured")
    message = EmailMessage()
    message["From"] = settings.smtp_from_email
    message["To"] = target_email
    message["Subject"] = subject
    message.set_content(message_text)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)


async def send_recruiter_email(target_email: str, message_text: str, subject: str) -> None:
    await asyncio.to_thread(_send_email_sync, target_email, message_text, subject)


async def create_vacancy_record(source_channel: str, recruiter_handle: str | None, vacancy_text: str) -> VacancyRecord | None:
    connection = get_db()
    existing = connection.execute(
        """
        SELECT *
        FROM vacancies
        WHERE source_channel = ? AND raw_text = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (source_channel, vacancy_text),
    ).fetchone()
    if existing is not None:
        connection.close()
        return None

    structured, ingestion_trace = run_ingestion_agent(vacancy_text, recruiter_handle)
    resolved_recruiter_handle = structured.get("recruiter_handle")
    recruiter_profile = None
    if resolved_recruiter_handle:
        ensure_recruiter(connection, resolved_recruiter_handle)
        recruiter_profile = fetch_recruiter_profile(connection, resolved_recruiter_handle)
    score, matched_skills, score_breakdown, filter_decision, filter_reasons, matching_trace = run_matching_decision_agent(structured, vacancy_text)
    context_bundle = build_context_bundle(
        structured=structured,
        raw_text=vacancy_text,
        matched_skills=matched_skills,
        filter_decision=filter_decision,
        filter_reasons=filter_reasons,
        recruiter_profile=recruiter_profile,
    )
    vacancy_id = str(uuid.uuid4())
    now = utc_now()
    draft_text = ""
    draft_source = "none"
    status = "manual_review"
    last_error = None
    approval_expires_at: str | None = None
    generation_trace: dict[str, Any] | None = None
    safety_trace: dict[str, Any] | None = None

    if filter_decision == "deny":
        draft_text = ""
        draft_source = "blocked_by_filters"
        status = "filtered_out"
    elif score >= settings.min_score:
        draft_text, draft_source, generation_trace = await run_generation_agent(structured, matched_skills, context_bundle)
        safety_decision, safety_trace = run_execution_safety_agent(
            connection=connection,
            structured=structured,
            filter_decision=filter_decision,
            filter_reasons=filter_reasons,
        )
        status = "awaiting_approval" if safety_decision == "allow" else "manual_review"
        approval_expires_at = compute_expiry(settings.approval_ttl_seconds)
    else:
        draft_text = fallback_draft(structured, matched_skills)
        draft_source = "fallback:below_score_threshold"
        status = "rejected_low_score" if filter_decision == "allow" else "manual_review"
        generation_trace = {
            "agent": "generation_agent",
            "status": "skipped",
            "reason": "below_score_threshold",
        }
        safety_trace = {
            "agent": "execution_safety_agent",
            "status": "skipped",
            "reason": "no_side_effect_path",
        }

    context_bundle["agent_trace"] = [
        trace for trace in (ingestion_trace, matching_trace, generation_trace, safety_trace) if trace is not None
    ]

    connection.execute(
        """
        INSERT INTO vacancies (
            id, source_channel, recruiter_handle, title, raw_text, structured_json,
            score, score_breakdown_json, filter_decision, filter_reasons_json,
            status, draft_text, draft_source, context_bundle_json, approval_expires_at,
            created_at, updated_at, last_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            vacancy_id,
            source_channel,
            resolved_recruiter_handle,
            structured["title"],
            vacancy_text,
            json.dumps(structured),
            score,
            json.dumps(score_breakdown),
            filter_decision,
            json.dumps(filter_reasons),
            status,
            draft_text,
            draft_source,
            json.dumps(context_bundle),
            approval_expires_at,
            now,
            now,
            last_error,
        ),
    )
    log_audit(
        connection,
        entity_type="vacancy",
        entity_id=vacancy_id,
        event_type="vacancy_ingested",
        payload={
            "source_channel": source_channel,
            "score": score,
            "matched_skills": matched_skills,
            "score_breakdown": score_breakdown,
            "filter_decision": filter_decision,
            "filter_reasons": filter_reasons,
            "draft_source": draft_source,
            "status": status,
            "agent_trace": context_bundle["agent_trace"],
        },
    )
    connection.commit()
    row = connection.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
    connection.close()
    record = vacancy_from_row(row)
    if record.filter_decision in {"allow", "manual_review"}:
        await send_operator_notification(record)
    return record


async def create_vacancy_records(source_channel: str, recruiter_handle: str | None, vacancy_text: str) -> list[VacancyRecord]:
    created: list[VacancyRecord] = []
    for chunk in split_vacancy_post(vacancy_text):
        record = await create_vacancy_record(source_channel, recruiter_handle, chunk)
        if record is not None:
            created.append(record)
    return created


def record_approval_event(
    connection: Any,
    *,
    vacancy_id: str,
    action: str,
    operator: str,
    note: str | None,
    edited_draft: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO approval_events (id, vacancy_id, action, operator, note, edited_draft, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            vacancy_id,
            action,
            operator,
            note,
            edited_draft,
            utc_now(),
        ),
    )


def record_dispatch_event(
    connection: Any,
    *,
    vacancy_id: str,
    recruiter_handle: str | None,
    contact_channel: str | None,
    contact_target: str | None,
    dispatch_mode: str,
    operator: str,
    outcome: str,
    note: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO dispatch_events (
            id, vacancy_id, recruiter_handle, contact_channel, contact_target,
            dispatch_mode, operator, outcome, note, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            vacancy_id,
            recruiter_handle,
            contact_channel,
            contact_target,
            dispatch_mode,
            operator,
            outcome,
            note,
            utc_now(),
        ),
    )


def is_legacy_row(row: dict[str, Any]) -> bool:
    try:
        score_breakdown = json.loads(row["score_breakdown_json"])
    except Exception:
        score_breakdown = {}
    try:
        structured = json.loads(row["structured_json"])
    except Exception:
        structured = {}

    extracted_handle, _ = extract_recruiter_handle(row["raw_text"], None)
    if not row["recruiter_handle"] and extracted_handle:
        return True
    if not score_breakdown:
        return True
    required_structured_fields = {
        "company",
        "location",
        "salary_min",
        "currency",
        "detected_roles",
        "skills",
        "contact_extraction_status",
        "contact_extraction_reason",
    }
    if not required_structured_fields.issubset(structured.keys()):
        return True
    try:
        context_bundle = json.loads(row["context_bundle_json"] or "{}")
    except Exception:
        context_bundle = {}
    return "agent_trace" not in context_bundle


def backfill_vacancy_row(connection: Any, row: dict[str, Any]) -> bool:
    if not is_legacy_row(row):
        return False

    structured, ingestion_trace = run_ingestion_agent(row["raw_text"], row["recruiter_handle"])
    score, matched_skills, score_breakdown, filter_decision, filter_reasons, matching_trace = run_matching_decision_agent(structured, row["raw_text"])
    resolved_recruiter_handle = structured.get("recruiter_handle")
    recruiter_profile = None
    if resolved_recruiter_handle:
        ensure_recruiter(connection, resolved_recruiter_handle)
        recruiter_profile = fetch_recruiter_profile(connection, resolved_recruiter_handle)
    context_bundle = build_context_bundle(
        structured=structured,
        raw_text=row["raw_text"],
        matched_skills=matched_skills,
        filter_decision=filter_decision,
        filter_reasons=filter_reasons,
        recruiter_profile=recruiter_profile,
    )
    context_bundle["agent_trace"] = [ingestion_trace, matching_trace]
    approval_expires_at = row["approval_expires_at"]

    current_status = row["status"]
    if current_status in {"sent_mock"}:
        current_status = "sent_dry_run"
    elif current_status == "awaiting_approval" and filter_decision == "deny":
        current_status = "filtered_out"
    elif current_status in {"rejected_low_score", "manual_review", "filtered_out", "awaiting_approval", "approved", "ready_to_send", "sent_dry_run", "sent_live", "rejected_by_operator", "send_failed"}:
        current_status = current_status
    if current_status in {"awaiting_approval", "manual_review", "approved", "ready_to_send"} and not approval_expires_at:
        approval_expires_at = compute_expiry(settings.approval_ttl_seconds)

    connection.execute(
        """
        UPDATE vacancies
        SET structured_json = ?,
            recruiter_handle = ?,
            score = ?,
            score_breakdown_json = ?,
            filter_decision = ?,
            filter_reasons_json = ?,
            context_bundle_json = ?,
            approval_expires_at = ?,
            status = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            json.dumps(structured),
            resolved_recruiter_handle,
            score,
            json.dumps(score_breakdown),
            filter_decision,
            json.dumps(filter_reasons),
            json.dumps(context_bundle),
            approval_expires_at,
            current_status,
            utc_now(),
            row["id"],
        ),
    )
    log_audit(
        connection,
        entity_type="vacancy",
        entity_id=row["id"],
        event_type="backfilled_legacy_vacancy",
        payload={
            "score": score,
            "matched_skills": matched_skills,
            "filter_decision": filter_decision,
            "status": current_status,
        },
    )
    return True


@app.middleware("http")
async def instrument_requests(request, call_next):
    endpoint = request.url.path
    started = time.perf_counter()
    response: Response | None = None
    try:
        response = await call_next(request)
        return response
    finally:
        elapsed = time.perf_counter() - started
        status_code = str(response.status_code if response is not None else 500)
        REQUEST_COUNT.labels(endpoint=endpoint, status_code=status_code).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint).observe(elapsed)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    connection = get_db()
    connection.execute("SELECT 1")
    connection.close()
    return {"status": "ok", "service": "tg-outreach-api"}


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    checks: dict[str, str] = {}
    connection = get_db()
    try:
        connection.execute("SELECT 1")
        checks["database"] = "ok"
    finally:
        connection.close()

    astrixa_health = probe_astrixa_health()
    checks["astrixa"] = str(astrixa_health["status"])

    status = "ok" if all(value == "ok" for value in checks.values()) else "degraded"
    return {
        "status": status,
        "service": "tg-outreach-api",
        "database_backend": database_backend_name(),
        "checks": checks,
    }


@app.get("/version")
def version() -> dict[str, str]:
    return {
        "service": "tg-outreach-api",
        "version": settings.build_version,
        "git_sha": settings.git_sha,
        "database_backend": database_backend_name(),
    }


@app.get("/api/v1/admin/runtime")
def admin_runtime() -> dict[str, Any]:
    connection = get_db()
    emergency_stop = get_emergency_stop_state(connection)
    worker_heartbeat, worker_heartbeat_updated_at = get_control_state_value(connection, "worker_heartbeat")
    applied_migrations = get_applied_migrations(connection)
    connection.close()

    heartbeat_age = age_seconds_from_iso(worker_heartbeat_updated_at)
    heartbeat_status = "missing"
    if heartbeat_age is not None:
        heartbeat_status = "ok" if heartbeat_age <= (settings.worker_poll_seconds * 3) else "stale"

    return {
        "service": "tg-outreach-api",
        "version": settings.build_version,
        "git_sha": settings.git_sha,
        "database_backend": database_backend_name(),
        "dispatch_mode": settings.dispatch_mode,
        "telegram_configured": telegram_runtime_configured(),
        "secret_status": {
            "astrixa_token_configured": bool(settings.astrixa_token),
            "telegram_session_configured": bool(settings.telegram_session_string),
            "smtp_configured": smtp_runtime_configured(),
        },
        "worker": {
            "worker_id": None if worker_heartbeat is None else worker_heartbeat.get("worker_id"),
            "last_heartbeat_at": worker_heartbeat_updated_at,
            "heartbeat_age_seconds": heartbeat_age,
            "status": heartbeat_status,
        },
        "migrations": {
            "applied_count": len(applied_migrations),
            "applied_versions": applied_migrations,
        },
        "emergency_stop": emergency_stop.model_dump(),
    }


@app.get("/api/v1/admin/dependencies")
def admin_dependencies() -> dict[str, Any]:
    connection = get_db()
    connection.execute("SELECT 1")
    applied_migrations = get_applied_migrations(connection)
    connection.close()

    return {
        "service": "tg-outreach-api",
        "database": {
            "backend": database_backend_name(),
            "status": "ok",
            "applied_migrations": applied_migrations,
        },
        "astrixa": {
            "base_url": settings.astrixa_base_url,
            "model": settings.astrixa_model,
            "health": probe_astrixa_health(),
            "invoke_probe": probe_astrixa_invoke(),
        },
    }


@app.get("/", include_in_schema=False)
def operator_console_root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/ui", include_in_schema=False)
def operator_console() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/metrics")
def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST)


@app.on_event("startup")
async def startup_validate_runtime() -> None:
    validate_runtime_config()


@app.get("/api/v1/config")
def get_config() -> dict[str, Any]:
    connection = get_db()
    emergency_stop = get_emergency_stop_state(connection)
    connection.close()
    return {
        "astrixa_base_url": settings.astrixa_base_url,
        "astrixa_model": settings.astrixa_model,
        "database_backend": database_backend_name(),
        "min_score": settings.min_score,
        "max_daily_outreach": settings.max_daily_outreach,
        "context_budget_tokens": settings.context_budget_tokens,
        "approval_ttl_seconds": settings.approval_ttl_seconds,
        "follow_up_delay_seconds": settings.follow_up_delay_seconds,
        "user_headline": CANDIDATE_PROFILE.get("headline", settings.user_headline),
        "user_skills": CANDIDATE_PROFILE.get("skills", settings.user_skills),
        "preferences": {
            "remote_only": CANDIDATE_PREFERENCES.get("remote_only"),
            "min_salary": CANDIDATE_PREFERENCES.get("min_salary"),
            "currency": CANDIDATE_PREFERENCES.get("currency"),
            "preferred_roles": CANDIDATE_PREFERENCES.get("preferred_roles", []),
        },
        "telegram_channels": settings.telegram_channels,
        "telegram_configured": telegram_runtime_configured(),
        "notify_target": settings.notify_target,
        "dispatch_mode": settings.dispatch_mode,
        "emergency_stop": emergency_stop.model_dump(),
    }


@app.post("/api/v1/vacancies/ingest", response_model=VacancyIngestResult)
async def ingest_vacancy(payload: VacancyIngestRequest) -> VacancyIngestResult:
    chunks = split_vacancy_post(payload.vacancy_text)
    records = await create_vacancy_records(
        payload.source_channel,
        payload.recruiter_handle,
        payload.vacancy_text,
    )
    if not records:
        raise HTTPException(status_code=409, detail="Duplicate vacancy")
    return VacancyIngestResult(
        source_channel=payload.source_channel,
        input_chunks=len(chunks),
        created_count=len(records),
        duplicate_count=max(0, len(chunks) - len(records)),
        created=records,
    )


@app.get("/api/v1/vacancies", response_model=list[VacancyRecord])
def list_vacancies() -> list[VacancyRecord]:
    connection = get_db()
    rows = connection.execute("SELECT * FROM vacancies ORDER BY created_at DESC").fetchall()
    connection.close()
    return [vacancy_from_row(row) for row in rows]


@app.get("/api/v1/conversations", response_model=list[ConversationRecord])
def list_conversations() -> list[ConversationRecord]:
    connection = get_db()
    rows = connection.execute("SELECT * FROM conversations ORDER BY updated_at DESC").fetchall()
    connection.close()
    return [conversation_from_row(row) for row in rows]


@app.get("/api/v1/recruiters", response_model=list[RecruiterRecord])
def list_recruiters() -> list[RecruiterRecord]:
    connection = get_db()
    rows = connection.execute("SELECT * FROM recruiters ORDER BY updated_at DESC").fetchall()
    connection.close()
    return [recruiter_from_row(row) for row in rows]


@app.get("/api/v1/recruiters/{recruiter_handle}/overview", response_model=RecruiterOverview)
def recruiter_overview(recruiter_handle: str) -> RecruiterOverview:
    normalized_handle = normalize_handle(recruiter_handle)
    if not normalized_handle:
        raise HTTPException(status_code=400, detail="Invalid recruiter_handle")

    connection = get_db()
    recruiter_row = connection.execute(
        "SELECT * FROM recruiters WHERE recruiter_handle = ?",
        (normalized_handle,),
    ).fetchone()
    if recruiter_row is None:
        connection.close()
        raise HTTPException(status_code=404, detail="Recruiter not found")

    conversation_row = connection.execute(
        "SELECT * FROM conversations WHERE recruiter_handle = ?",
        (normalized_handle,),
    ).fetchone()
    vacancy_rows = connection.execute(
        "SELECT * FROM vacancies WHERE recruiter_handle = ? ORDER BY created_at DESC",
        (normalized_handle,),
    ).fetchall()

    conversation_summaries: list[ConversationSummaryRecord] = []
    outreach_attempts: list[dict[str, Any]] = []
    if conversation_row is not None:
        summary_rows = connection.execute(
            """
            SELECT * FROM conversation_summaries
            WHERE conversation_id = ?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (conversation_row["id"],),
        ).fetchall()
        conversation_summaries = [
            ConversationSummaryRecord(
                id=row["id"],
                conversation_id=row["conversation_id"],
                summary_text=row["summary_text"],
                source_event=row["source_event"],
                created_at=row["created_at"],
            )
            for row in summary_rows
        ]
        outreach_attempt_rows = connection.execute(
            """
            SELECT * FROM outreach_attempts
            WHERE conversation_id = ?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (conversation_row["id"],),
        ).fetchall()
        outreach_attempts = [
            {
                "id": row["id"],
                "vacancy_id": row["vacancy_id"],
                "attempt_type": row["attempt_type"],
                "outcome": row["outcome"],
                "draft_text": row["draft_text"],
                "created_at": row["created_at"],
            }
            for row in outreach_attempt_rows
        ]

    dispatch_rows = connection.execute(
        """
        SELECT * FROM dispatch_events
        WHERE recruiter_handle = ?
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (normalized_handle,),
    ).fetchall()
    dispatch_events = [
        {
            "id": row["id"],
            "vacancy_id": row["vacancy_id"],
            "contact_channel": row["contact_channel"],
            "contact_target": row["contact_target"],
            "dispatch_mode": row["dispatch_mode"],
            "operator": row["operator"],
            "outcome": row["outcome"],
            "note": row["note"],
            "created_at": row["created_at"],
        }
        for row in dispatch_rows
    ]

    entity_ids = [row["id"] for row in vacancy_rows]
    if conversation_row is not None:
        entity_ids.append(conversation_row["id"])
    timeline_rows: list[dict[str, Any]] = []
    if entity_ids:
        placeholders = ",".join("?" for _ in entity_ids)
        timeline_rows = connection.execute(
            f"""
            SELECT * FROM audit_events
            WHERE entity_id IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT 50
            """,
            entity_ids,
        ).fetchall()
    connection.close()

    timeline = [
        TimelineEventRecord(
            source="audit_event",
            event_type=row["event_type"],
            entity_id=row["entity_id"],
            created_at=row["created_at"],
            payload=json.loads(row["payload_json"] or "{}"),
        )
        for row in timeline_rows
    ]

    return RecruiterOverview(
        recruiter=recruiter_from_row(recruiter_row),
        conversation=None if conversation_row is None else conversation_from_row(conversation_row),
        vacancies=[vacancy_from_row(row) for row in vacancy_rows],
        conversation_summaries=conversation_summaries,
        outreach_attempts=outreach_attempts,
        dispatch_events=dispatch_events,
        timeline=timeline,
    )


@app.get("/api/v1/conversations/{conversation_id}/timeline", response_model=list[ConversationTimelineItem])
def conversation_timeline(conversation_id: str) -> list[ConversationTimelineItem]:
    connection = get_db()
    conversation_row = connection.execute(
        "SELECT * FROM conversations WHERE id = ?",
        (conversation_id,),
    ).fetchone()
    if conversation_row is None:
        connection.close()
        raise HTTPException(status_code=404, detail="Conversation not found")

    recruiter_handle = str(conversation_row["recruiter_handle"])

    inbound_rows = connection.execute(
        """
        SELECT * FROM inbound_message_events
        WHERE recruiter_handle = ?
        ORDER BY created_at DESC
        LIMIT 50
        """,
        (recruiter_handle,),
    ).fetchall()
    attempt_rows = connection.execute(
        """
        SELECT * FROM outreach_attempts
        WHERE conversation_id = ?
        ORDER BY created_at DESC
        LIMIT 50
        """,
        (conversation_id,),
    ).fetchall()
    summary_rows = connection.execute(
        """
        SELECT * FROM conversation_summaries
        WHERE conversation_id = ?
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (conversation_id,),
    ).fetchall()
    dispatch_rows = connection.execute(
        """
        SELECT * FROM dispatch_events
        WHERE recruiter_handle = ?
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (recruiter_handle,),
    ).fetchall()
    audit_rows = connection.execute(
        """
        SELECT * FROM audit_events
        WHERE entity_id = ?
        ORDER BY created_at DESC
        LIMIT 30
        """,
        (conversation_id,),
    ).fetchall()
    connection.close()

    items: list[ConversationTimelineItem] = []
    for row in inbound_rows:
        raw_text = str(row["raw_text"])
        items.append(
            ConversationTimelineItem(
                kind="inbound_message",
                created_at=row["created_at"],
                direction="inbound",
                source=row["source"],
                summary=raw_text[:180],
                details={
                    "id": row["id"],
                    "external_message_id": row["external_message_id"],
                    "raw_text": raw_text,
                },
            )
        )

    for row in attempt_rows:
        draft_text = str(row["draft_text"])
        items.append(
            ConversationTimelineItem(
                kind="outreach_attempt",
                created_at=row["created_at"],
                direction="outbound",
                source=row["attempt_type"],
                summary=f"{row['attempt_type']} / {row['outcome']}",
                details={
                    "id": row["id"],
                    "vacancy_id": row["vacancy_id"],
                    "attempt_type": row["attempt_type"],
                    "outcome": row["outcome"],
                    "draft_text": draft_text,
                },
            )
        )

    for row in summary_rows:
        items.append(
            ConversationTimelineItem(
                kind="conversation_summary",
                created_at=row["created_at"],
                direction=None,
                source=row["source_event"],
                summary=str(row["summary_text"])[:180],
                details={
                    "id": row["id"],
                    "source_event": row["source_event"],
                    "summary_text": row["summary_text"],
                },
            )
        )

    for row in dispatch_rows:
        items.append(
            ConversationTimelineItem(
                kind="dispatch_event",
                created_at=row["created_at"],
                direction="outbound",
                source=row["dispatch_mode"],
                summary=f"{row['outcome']} via {row['dispatch_mode']}",
                details={
                    "id": row["id"],
                    "vacancy_id": row["vacancy_id"],
                    "dispatch_mode": row["dispatch_mode"],
                    "operator": row["operator"],
                    "note": row["note"],
                    "outcome": row["outcome"],
                },
            )
        )

    for row in audit_rows:
        payload = json.loads(row["payload_json"] or "{}")
        items.append(
            ConversationTimelineItem(
                kind="audit_event",
                created_at=row["created_at"],
                direction=None,
                source="audit",
                summary=str(row["event_type"]),
                details={
                    "id": row["id"],
                    "event_type": row["event_type"],
                    "payload": payload,
                },
            )
        )

    items.sort(key=lambda item: item.created_at, reverse=True)
    return items


@app.get("/api/v1/conversations/{conversation_id}/summaries", response_model=list[ConversationSummaryRecord])
def list_conversation_summaries(conversation_id: str) -> list[ConversationSummaryRecord]:
    connection = get_db()
    rows = connection.execute(
        """
        SELECT * FROM conversation_summaries
        WHERE conversation_id = ?
        ORDER BY created_at DESC
        """,
        (conversation_id,),
    ).fetchall()
    connection.close()
    return [
        ConversationSummaryRecord(
            id=row["id"],
            conversation_id=row["conversation_id"],
            summary_text=row["summary_text"],
            source_event=row["source_event"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


@app.get("/api/v1/memory-documents", response_model=list[MemoryDocumentRecord])
def list_memory_documents() -> list[MemoryDocumentRecord]:
    connection = get_db()
    rows = connection.execute(
        "SELECT * FROM memory_documents ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    connection.close()
    return [
        MemoryDocumentRecord(
            id=row["id"],
            memory_type=row["memory_type"],
            entity_id=row["entity_id"],
            content_text=row["content_text"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            created_at=row["created_at"],
        )
        for row in rows
    ]


@app.get("/api/v1/jobs", response_model=list[JobRecord])
def list_jobs() -> list[JobRecord]:
    connection = get_db()
    rows = connection.execute("SELECT * FROM jobs ORDER BY run_at ASC, created_at ASC").fetchall()
    connection.close()
    return [job_from_row(row) for row in rows]


@app.get("/api/v1/ops/failed-jobs", response_model=list[JobFailureRecord])
def list_failed_jobs(limit: int = 20) -> list[JobFailureRecord]:
    safe_limit = max(1, min(limit, 100))
    connection = get_db()
    rows = connection.execute(
        """
        SELECT *
        FROM jobs
        WHERE status = 'failed'
        ORDER BY updated_at DESC, created_at DESC
        LIMIT ?
        """,
        (safe_limit,),
    ).fetchall()
    connection.close()
    return [
        JobFailureRecord(
            id=row["id"],
            job_type=row["job_type"],
            entity_id=row["entity_id"],
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            run_at=row["run_at"],
            last_error=row["last_error"],
            updated_at=row["updated_at"],
        )
        for row in rows
    ]


@app.get("/api/v1/ops/summary", response_model=OpsSummary)
def ops_summary() -> OpsSummary:
    connection = get_db()
    now_iso = utc_now()
    job_rows = connection.execute("SELECT * FROM jobs").fetchall()
    recruiter_rows = connection.execute("SELECT * FROM recruiters").fetchall()
    conversation_rows = connection.execute("SELECT * FROM conversations").fetchall()
    vacancy_rows = connection.execute("SELECT structured_json, draft_source FROM vacancies").fetchall()
    worker_heartbeat, worker_heartbeat_updated_at = get_control_state_value(connection, "worker_heartbeat")
    connection.close()

    jobs_by_status: dict[str, int] = {}
    jobs_by_type: dict[str, int] = {}
    due_pending_jobs = 0
    overdue_leased_jobs = 0
    failed_jobs = 0
    oldest_pending_job_age_seconds: int | None = None

    for row in job_rows:
        status = str(row["status"])
        job_type = str(row["job_type"])
        jobs_by_status[status] = jobs_by_status.get(status, 0) + 1
        jobs_by_type[job_type] = jobs_by_type.get(job_type, 0) + 1
        if status == "failed":
            failed_jobs += 1
        if status == "pending" and row["run_at"] <= now_iso:
            due_pending_jobs += 1
        if status == "leased" and (not row["lease_expires_at"] or row["lease_expires_at"] <= now_iso):
            overdue_leased_jobs += 1
        if status == "pending":
            age_seconds = age_seconds_from_iso(row["created_at"])
            if age_seconds is not None and (
                oldest_pending_job_age_seconds is None or age_seconds > oldest_pending_job_age_seconds
            ):
                oldest_pending_job_age_seconds = age_seconds

    recruiters_by_status: dict[str, int] = {}
    contacted_without_reply = 0
    for row in recruiter_rows:
        status = str(row["status"])
        recruiters_by_status[status] = recruiters_by_status.get(status, 0) + 1
        if int(row["outbound_count"]) > 0 and int(row["inbound_count"]) == 0:
            contacted_without_reply += 1

    conversations_by_status: dict[str, int] = {}
    for row in conversation_rows:
        status = str(row["status"])
        conversations_by_status[status] = conversations_by_status.get(status, 0) + 1

    contact_extraction_by_status: dict[str, int] = {}
    contact_extraction_by_reason: dict[str, int] = {}
    generation_sources: dict[str, int] = {}
    fallback_generations = 0
    for row in vacancy_rows:
        structured = json.loads(row["structured_json"] or "{}")
        extraction_status = str(structured.get("contact_extraction_status") or "unknown")
        extraction_reason = str(structured.get("contact_extraction_reason") or "unknown")
        draft_source = str(row["draft_source"] or "unknown")
        contact_extraction_by_status[extraction_status] = contact_extraction_by_status.get(extraction_status, 0) + 1
        contact_extraction_by_reason[extraction_reason] = contact_extraction_by_reason.get(extraction_reason, 0) + 1
        generation_sources[draft_source] = generation_sources.get(draft_source, 0) + 1
        if draft_source.startswith("fallback:"):
            fallback_generations += 1

    worker_heartbeat_age_seconds = age_seconds_from_iso(worker_heartbeat_updated_at)
    worker_status = "missing"
    if worker_heartbeat_age_seconds is not None:
        worker_status = "ok" if worker_heartbeat_age_seconds <= (settings.worker_poll_seconds * 3) else "stale"

    astrixa_health = probe_astrixa_health()
    astrixa_invoke = probe_astrixa_invoke()
    astrixa_health_status = str(astrixa_health.get("status", "unknown"))
    astrixa_invoke_status = str(astrixa_invoke.get("status", "unknown"))
    dependency_degraded = (
        worker_status != "ok"
        or astrixa_health_status != "ok"
        or astrixa_invoke_status != "ok"
    )

    return OpsSummary(
        total_jobs=len(job_rows),
        jobs_by_status=jobs_by_status,
        jobs_by_type=jobs_by_type,
        due_pending_jobs=due_pending_jobs,
        overdue_leased_jobs=overdue_leased_jobs,
        failed_jobs=failed_jobs,
        oldest_pending_job_age_seconds=oldest_pending_job_age_seconds,
        total_recruiters=len(recruiter_rows),
        recruiters_by_status=recruiters_by_status,
        contacted_without_reply=contacted_without_reply,
        total_conversations=len(conversation_rows),
        conversations_by_status=conversations_by_status,
        contact_extraction_by_status=contact_extraction_by_status,
        contact_extraction_by_reason=contact_extraction_by_reason,
        generation_sources=generation_sources,
        fallback_generations=fallback_generations,
        worker_status=worker_status,
        worker_heartbeat_age_seconds=worker_heartbeat_age_seconds,
        astrixa_health_status=astrixa_health_status,
        astrixa_invoke_status=astrixa_invoke_status,
        dependency_degraded=dependency_degraded,
    )


@app.get("/api/v1/dashboard/summary", response_model=DashboardSummary)
def dashboard_summary() -> DashboardSummary:
    connection = get_db()
    total_vacancies = connection.execute("SELECT COUNT(*) AS total FROM vacancies").fetchone()["total"]

    by_status_rows = connection.execute(
        "SELECT status, COUNT(*) AS total FROM vacancies GROUP BY status ORDER BY total DESC"
    ).fetchall()
    by_filter_rows = connection.execute(
        "SELECT filter_decision, COUNT(*) AS total FROM vacancies GROUP BY filter_decision ORDER BY total DESC"
    ).fetchall()
    top_channel_rows = connection.execute(
        "SELECT source_channel, COUNT(*) AS total FROM vacancies GROUP BY source_channel ORDER BY total DESC LIMIT 10"
    ).fetchall()
    connection.close()

    return DashboardSummary(
        total_vacancies=total_vacancies,
        by_status={row["status"]: row["total"] for row in by_status_rows},
        by_filter_decision={row["filter_decision"]: row["total"] for row in by_filter_rows},
        top_channels={row["source_channel"]: row["total"] for row in top_channel_rows},
    )


@app.get("/api/v1/dashboard/review", response_model=ReviewBoard)
def dashboard_review() -> ReviewBoard:
    connection = get_db()
    rows = connection.execute("SELECT * FROM vacancies ORDER BY created_at DESC").fetchall()
    connection.close()

    grouped: dict[str, list[VacancyRecord]] = {}
    preferred_order = [
        "awaiting_approval",
        "approved",
        "ready_to_send",
        "sent_dry_run",
        "sent_live",
        "filtered_out",
        "manual_review",
        "rejected_low_score",
        "rejected_by_operator",
        "send_failed",
    ]

    for row in rows:
        record = vacancy_from_row(row)
        grouped.setdefault(record.status, []).append(record)

    ordered_statuses = [status for status in preferred_order if status in grouped]
    ordered_statuses.extend(status for status in grouped if status not in ordered_statuses)

    return ReviewBoard(
        groups=[
            ReviewGroup(status=status, items=grouped[status])
            for status in ordered_statuses
        ]
    )


@app.post("/api/v1/admin/backfill", response_model=BackfillResult)
def backfill_legacy_records() -> BackfillResult:
    connection = get_db()
    rows = connection.execute("SELECT * FROM vacancies ORDER BY created_at ASC").fetchall()

    scanned = len(rows)
    updated = 0
    skipped = 0
    for row in rows:
        changed = backfill_vacancy_row(connection, row)
        if changed:
            updated += 1
        else:
            skipped += 1

    connection.commit()
    connection.close()
    return BackfillResult(scanned=scanned, updated=updated, skipped=skipped)


@app.get("/api/v1/control/emergency-stop", response_model=EmergencyStopState)
def get_emergency_stop() -> EmergencyStopState:
    connection = get_db()
    state = get_emergency_stop_state(connection)
    connection.close()
    return state


@app.post("/api/v1/control/emergency-stop", response_model=EmergencyStopState)
def set_emergency_stop(payload: EmergencyStopRequest) -> EmergencyStopState:
    connection = get_db()
    state = set_emergency_stop_state(
        connection,
        enabled=payload.enabled,
        operator=payload.operator,
        reason=payload.reason,
    )
    log_audit(
        connection,
        entity_type="control_state",
        entity_id="emergency_stop",
        event_type="emergency_stop_updated",
        payload=state.model_dump(),
    )
    connection.commit()
    connection.close()
    return state


@app.get("/api/v1/vacancies/{vacancy_id}", response_model=VacancyRecord)
def get_vacancy(vacancy_id: str) -> VacancyRecord:
    connection = get_db()
    row = connection.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
    connection.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Vacancy not found")
    return vacancy_from_row(row)


@app.post("/api/v1/vacancies/{vacancy_id}/approve", response_model=VacancyRecord)
def approve_vacancy(vacancy_id: str, payload: ApprovalRequest) -> VacancyRecord:
    connection = get_db()
    assert_emergency_stop_not_enabled(connection, "approve")
    row = connection.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
    if row is None:
        connection.close()
        raise HTTPException(status_code=404, detail="Vacancy not found")
    if row["status"] not in {"awaiting_approval", "manual_review"}:
        connection.close()
        raise HTTPException(status_code=409, detail="Vacancy is not approvable")

    new_draft = payload.edited_draft.strip() if payload.edited_draft else row["draft_text"]
    new_status = "approved"
    updated_at = utc_now()
    approval_expires_at = compute_expiry(settings.approval_ttl_seconds)
    structured = json.loads(row["structured_json"] or "{}")
    connection.execute(
        """
        UPDATE vacancies
        SET status = ?, draft_text = ?, approval_expires_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_status, new_draft, approval_expires_at, updated_at, vacancy_id),
    )
    record_approval_event(
        connection,
        vacancy_id=vacancy_id,
        action="approve",
        operator=payload.operator,
        note=payload.note,
        edited_draft=payload.edited_draft,
    )
    promote_approved_outreach_snippet(
        connection,
        vacancy_id=vacancy_id,
        recruiter_handle=row["recruiter_handle"],
        structured=structured,
        approved_draft=new_draft,
    )
    log_audit(
        connection,
        entity_type="vacancy",
        entity_id=vacancy_id,
        event_type="approved",
        payload={"operator": payload.operator, "note": payload.note, "memory_promoted": True},
    )
    connection.commit()
    updated_row = connection.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
    connection.close()
    return vacancy_from_row(updated_row)


@app.post("/api/v1/vacancies/{vacancy_id}/queue-send", response_model=VacancyRecord)
def queue_vacancy_for_dispatch(vacancy_id: str, payload: QueueDispatchRequest) -> VacancyRecord:
    connection = get_db()
    assert_emergency_stop_not_enabled(connection, "queue_send")
    row = connection.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
    if row is None:
        connection.close()
        raise HTTPException(status_code=404, detail="Vacancy not found")
    if row["status"] != "approved":
        connection.close()
        raise HTTPException(status_code=409, detail="Vacancy is not ready to queue for dispatch")
    if is_expired(row["approval_expires_at"]):
        updated_at = utc_now()
        connection.execute(
            "UPDATE vacancies SET status = ?, updated_at = ? WHERE id = ?",
            ("manual_review", updated_at, vacancy_id),
        )
        connection.commit()
        connection.close()
        raise HTTPException(status_code=409, detail="Approval expired; re-approval required")

    updated_at = utc_now()
    connection.execute(
        "UPDATE vacancies SET status = ?, updated_at = ? WHERE id = ?",
        ("ready_to_send", updated_at, vacancy_id),
        )
    log_audit(
        connection,
        entity_type="vacancy",
        entity_id=vacancy_id,
        event_type="queued_for_dispatch",
        payload={"operator": payload.operator, "note": payload.note},
    )
    connection.commit()
    updated_row = connection.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
    connection.close()
    return vacancy_from_row(updated_row)


@app.post("/api/v1/vacancies/{vacancy_id}/reject", response_model=VacancyRecord)
def reject_vacancy(vacancy_id: str, payload: ApprovalRequest) -> VacancyRecord:
    connection = get_db()
    row = connection.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
    if row is None:
        connection.close()
        raise HTTPException(status_code=404, detail="Vacancy not found")
    if row["status"] not in {"awaiting_approval", "manual_review", "approved"}:
        connection.close()
        raise HTTPException(status_code=409, detail="Vacancy is not rejectable")

    updated_at = utc_now()
    connection.execute(
        "UPDATE vacancies SET status = ?, updated_at = ? WHERE id = ?",
        ("rejected_by_operator", updated_at, vacancy_id),
        )
    record_approval_event(
        connection,
        vacancy_id=vacancy_id,
        action="reject",
        operator=payload.operator,
        note=payload.note,
        edited_draft=None,
    )
    log_audit(
        connection,
        entity_type="vacancy",
        entity_id=vacancy_id,
        event_type="rejected_by_operator",
        payload={"operator": payload.operator, "note": payload.note},
    )
    connection.commit()
    updated_row = connection.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
    connection.close()
    return vacancy_from_row(updated_row)


@app.post("/api/v1/vacancies/{vacancy_id}/edit", response_model=VacancyRecord)
def edit_vacancy_draft(vacancy_id: str, payload: ApprovalRequest) -> VacancyRecord:
    if not payload.edited_draft or not payload.edited_draft.strip():
        raise HTTPException(status_code=400, detail="edited_draft is required")

    connection = get_db()
    row = connection.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
    if row is None:
        connection.close()
        raise HTTPException(status_code=404, detail="Vacancy not found")
    if row["status"] not in {"awaiting_approval", "manual_review", "approved"}:
        connection.close()
        raise HTTPException(status_code=409, detail="Vacancy draft is not editable")

    updated_at = utc_now()
    new_draft = payload.edited_draft.strip()[:500]
    connection.execute(
        "UPDATE vacancies SET draft_text = ?, updated_at = ? WHERE id = ?",
        (new_draft, updated_at, vacancy_id),
    )
    record_approval_event(
        connection,
        vacancy_id=vacancy_id,
        action="edit",
        operator=payload.operator,
        note=payload.note,
        edited_draft=new_draft,
    )
    log_audit(
        connection,
        entity_type="vacancy",
        entity_id=vacancy_id,
        event_type="draft_edited",
        payload={"operator": payload.operator, "note": payload.note},
    )
    connection.commit()
    updated_row = connection.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
    connection.close()
    return vacancy_from_row(updated_row)


@app.post("/api/v1/telegram/ingest", response_model=TelegramIngestResult)
async def ingest_from_telegram(payload: TelegramIngestRequest) -> TelegramIngestResult:
    if not settings.telegram_api_id or not settings.telegram_api_hash or not settings.telegram_session_string:
        raise HTTPException(status_code=503, detail="Telegram client is not configured")
    if not settings.telegram_channels:
        raise HTTPException(status_code=503, detail="Telegram channels are not configured")

    fetched_messages = 0
    created_vacancies = 0
    skipped_duplicates = 0

    async with TelegramClient(
        StringSession(settings.telegram_session_string),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    ) as client:
        for channel in settings.telegram_channels:
            entity = await client.get_entity(channel)
            async for message in client.iter_messages(entity, limit=payload.per_channel_limit):
                text = (message.message or "").strip()
                if len(text) < 20:
                    continue
                fetched_messages += 1
                records = await create_vacancy_records(channel, None, text)
                if not records:
                    skipped_duplicates += 1
                else:
                    created_vacancies += len(records)

    return TelegramIngestResult(
        configured_channels=settings.telegram_channels,
        processed_channels=len(settings.telegram_channels),
        fetched_messages=fetched_messages,
        created_vacancies=created_vacancies,
        skipped_duplicates=skipped_duplicates,
    )


@app.post("/api/v1/conversations/reply", response_model=ConversationReplyResult)
async def ingest_recruiter_reply(payload: ConversationReplyRequest) -> ConversationReplyResult:
    connection = get_db()
    result = ingest_recruiter_reply_internal(
        connection,
        recruiter_handle=payload.recruiter_handle,
        message_text=payload.message_text,
        source=payload.source,
    )
    connection.commit()
    connection.close()
    await refresh_conversation_memory(
        conversation_id=result.conversation.id,
        recruiter_handle=result.conversation.recruiter_handle,
        latest_message=payload.message_text,
        latest_classification=result.classification,
        source_event=payload.source,
    )
    return result


@app.post("/api/v1/telegram/replies/poll", response_model=TelegramReplyPollResult)
async def poll_telegram_replies(payload: TelegramReplyPollRequest) -> TelegramReplyPollResult:
    if not settings.telegram_api_id or not settings.telegram_api_hash or not settings.telegram_session_string:
        raise HTTPException(status_code=503, detail="Telegram client is not configured")
    return await poll_telegram_replies_internal(payload.per_conversation_limit)


@app.post("/api/v1/admin/seed-worker-jobs", response_model=WorkerSeedResult)
def seed_worker_jobs() -> WorkerSeedResult:
    connection = get_db()
    seeded: list[str] = []
    seeded.append(
        upsert_periodic_job(
            connection,
            job_type="telegram_reply_poll",
            entity_id="global",
            interval_seconds=settings.telegram_reply_poll_interval_seconds,
            payload={"per_conversation_limit": 5},
            max_attempts=3,
        )
    )
    log_audit(
        connection,
        entity_type="job_execution",
        entity_id="telegram_reply_poll",
        event_type="worker_jobs_seeded",
        payload={"seeded_jobs": seeded},
    )
    connection.commit()
    connection.close()
    return WorkerSeedResult(seeded_jobs=seeded)


@app.post("/api/v1/vacancies/{vacancy_id}/dispatch", response_model=VacancyRecord)
async def dispatch_vacancy(vacancy_id: str, payload: DispatchRequest) -> VacancyRecord:
    connection = get_db()
    assert_emergency_stop_not_enabled(connection, "dispatch")
    row = connection.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
    if row is None:
        connection.close()
        raise HTTPException(status_code=404, detail="Vacancy not found")
    if row["status"] != "ready_to_send":
        connection.close()
        raise HTTPException(status_code=409, detail="Vacancy is not dispatchable")
    if is_expired(row["approval_expires_at"]):
        updated_at = utc_now()
        connection.execute(
            "UPDATE vacancies SET status = ?, updated_at = ? WHERE id = ?",
            ("manual_review", updated_at, vacancy_id),
        )
        connection.commit()
        connection.close()
        raise HTTPException(status_code=409, detail="Approval expired; re-approval required")

    sent_today = connection.execute(
        """
        SELECT COUNT(*) AS total
        FROM dispatch_events
        WHERE outcome IN ('sent_dry_run', 'sent_live')
          AND substr(created_at, 1, 10) = substr(?, 1, 10)
        """,
        (utc_now(),),
    ).fetchone()["total"]
    if sent_today >= settings.max_daily_outreach:
        connection.close()
        raise HTTPException(status_code=409, detail="Daily outreach limit reached")

    structured = json.loads(row["structured_json"] or "{}")
    recruiter_handle = row["recruiter_handle"]
    contact_email = structured.get("contact_email")
    contact_channel, contact_target = resolve_contact_channel(contact_email, recruiter_handle)
    if contact_target:
        previous_send = connection.execute(
            """
            SELECT id
            FROM dispatch_events
            WHERE contact_target = ?
              AND outcome IN ('sent_dry_run', 'sent_live')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (contact_target,),
        ).fetchone()
        if previous_send is not None:
            connection.close()
            raise HTTPException(status_code=409, detail="Dispatch already exists for this contact target")

    status = "sent_dry_run"
    event_type = "dispatch_dry_run"
    if settings.dispatch_mode == "manual_send":
        if not contact_target or not contact_channel:
            connection.close()
            raise HTTPException(status_code=409, detail="Contact target is required for live dispatch")
        try:
            if contact_channel == "email":
                await send_recruiter_email(contact_target, row["draft_text"], row["title"])
            elif contact_channel == "telegram":
                if not settings.telegram_api_id or not settings.telegram_api_hash or not settings.telegram_session_string:
                    connection.close()
                    raise HTTPException(status_code=503, detail="Telegram client is not configured for live dispatch")
                await send_recruiter_message(contact_target, row["draft_text"])
            else:
                connection.close()
                raise HTTPException(status_code=409, detail="Unsupported contact channel")
        except Exception as exc:
            updated_at = utc_now()
            connection.execute(
                "UPDATE vacancies SET status = ?, updated_at = ?, last_error = ? WHERE id = ?",
                ("send_failed", updated_at, str(exc), vacancy_id),
            )
            record_dispatch_event(
                connection,
                vacancy_id=vacancy_id,
                recruiter_handle=recruiter_handle,
                contact_channel=contact_channel,
                contact_target=contact_target,
                dispatch_mode=settings.dispatch_mode,
                operator=payload.operator,
                outcome="send_failed",
                note=payload.note,
            )
            log_audit(
                connection,
                entity_type="vacancy",
                entity_id=vacancy_id,
                event_type="dispatch_failed",
                payload={"operator": payload.operator, "note": payload.note, "error": str(exc)},
            )
            connection.commit()
            failed_row = connection.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
            connection.close()
            return vacancy_from_row(failed_row)
        status = "sent_live"
        event_type = "dispatch_live"

    updated_at = utc_now()
    connection.execute(
        "UPDATE vacancies SET status = ?, updated_at = ?, last_error = NULL WHERE id = ?",
        (status, updated_at, vacancy_id),
    )
    conversation_id = None
    if recruiter_handle:
        conversation_id = ensure_conversation(connection, recruiter_handle)
        update_recruiter_outbound(connection, recruiter_handle)
        connection.execute(
            """
            UPDATE conversations
            SET status = ?, last_outbound_at = ?, updated_at = ?
            WHERE id = ?
            """,
            ("waiting_reply", updated_at, updated_at, conversation_id),
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
                conversation_id,
                "initial_outreach",
                status,
                row["draft_text"],
                updated_at,
            ),
        )
        follow_up_run_at = compute_expiry(settings.follow_up_delay_seconds)
        schedule_job(
            connection,
            job_type="follow_up_due",
            entity_id=conversation_id,
            run_at=follow_up_run_at,
            payload={
                "vacancy_id": vacancy_id,
                "recruiter_handle": recruiter_handle,
                "conversation_id": conversation_id,
                "source_attempt": "initial_outreach",
            },
            max_attempts=2,
        )
    record_dispatch_event(
        connection,
        vacancy_id=vacancy_id,
        recruiter_handle=recruiter_handle,
        contact_channel=contact_channel,
        contact_target=contact_target,
        dispatch_mode=settings.dispatch_mode,
        operator=payload.operator,
        outcome=status,
        note=payload.note,
    )
    log_audit(
        connection,
        entity_type="vacancy",
        entity_id=vacancy_id,
        event_type=event_type,
        payload={
            "operator": payload.operator,
            "note": payload.note,
            "dispatch_mode": settings.dispatch_mode,
            "contact_channel": contact_channel,
            "contact_target": contact_target,
            "conversation_id": conversation_id,
        },
    )
    connection.commit()
    updated_row = connection.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()
    connection.close()
    return vacancy_from_row(updated_row)
