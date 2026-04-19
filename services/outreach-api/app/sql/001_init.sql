CREATE TABLE IF NOT EXISTS vacancies (
    id TEXT PRIMARY KEY,
    source_channel TEXT NOT NULL,
    recruiter_handle TEXT,
    title TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    structured_json TEXT NOT NULL,
    score REAL NOT NULL,
    score_breakdown_json TEXT NOT NULL DEFAULT '{}',
    filter_decision TEXT NOT NULL DEFAULT 'manual_review',
    filter_reasons_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    draft_text TEXT NOT NULL,
    draft_source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_events (
    dedupe_key TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    target TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_events (
    id TEXT PRIMARY KEY,
    vacancy_id TEXT NOT NULL,
    action TEXT NOT NULL,
    operator TEXT NOT NULL,
    note TEXT,
    edited_draft TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatch_events (
    id TEXT PRIMARY KEY,
    vacancy_id TEXT NOT NULL,
    recruiter_handle TEXT,
    contact_channel TEXT,
    contact_target TEXT,
    dispatch_mode TEXT NOT NULL,
    operator TEXT NOT NULL,
    outcome TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    recruiter_handle TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    last_outbound_at TEXT,
    last_inbound_at TEXT,
    rejection_flag INTEGER NOT NULL DEFAULT 0,
    follow_up_sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recruiters (
    id TEXT PRIMARY KEY,
    recruiter_handle TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_outbound_at TEXT,
    last_inbound_at TEXT,
    outbound_count INTEGER NOT NULL DEFAULT 0,
    inbound_count INTEGER NOT NULL DEFAULT 0,
    positive_reply_count INTEGER NOT NULL DEFAULT 0,
    negative_reply_count INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outreach_attempts (
    id TEXT PRIMARY KEY,
    vacancy_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    attempt_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    draft_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    source_event TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_documents (
    id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    content_text TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    status TEXT NOT NULL,
    run_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    payload_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbound_message_events (
    id TEXT PRIMARY KEY,
    recruiter_handle TEXT NOT NULL,
    source TEXT NOT NULL,
    external_message_id TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(recruiter_handle, source, external_message_id)
);

CREATE TABLE IF NOT EXISTS control_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
