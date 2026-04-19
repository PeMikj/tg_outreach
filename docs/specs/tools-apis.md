# Tools and APIs Spec

## Purpose

Определяет единый контракт интеграционных адаптеров: Telegram, SMTP, Astrixa gateway,
scheduler/clock и local config/storage.

## Common Adapter Rules

- Every call has correlation id
- Every call emits metrics and auditable outcome metadata
- Errors are normalized into:
  - `transient`
  - `rate_limited`
  - `permanent`
  - `invalid_request`
  - `unavailable`
- Each call has explicit timeout and retry budget

## Telegram Adapter

Operations:

- `poll_channel_updates(cursor)`
- `poll_conversation_updates(cursor)`
- `send_message(conversation_key, message_text, idempotency_key)`

Constraints:

- Send timeout: 10 s
- Poll timeout: 30 s
- Max retries for transient send errors: 2

Side effects:

- Real outbound Telegram message

Protections:

- cooldown on flood/rate-limit error
- idempotency key required for send
- duplicate recipient guard

## SMTP Email Adapter

Operations:

- `send_email(contact_email, subject, body_text, idempotency_key)`

Constraints:

- Send timeout depends on SMTP server
- No live send when dispatch mode = `dry_run`
- Requires explicit SMTP runtime configuration

Side effects:

- Real outbound email in `manual_send`

Protections:

- explicit target required
- duplicate recipient guard
- approval and policy checks happen before adapter invocation

## Astrixa Adapter

Operations:

- `chat_completion(messages, model, metadata)`
- `draft_generation(structured_vacancy, context_bundle, constraints)`
- `reply_classification(reply_text, conversation_summary)`
- `conversation_summary(messages)`

Constraints:

- Timeout: 6 s default, 10 s hard max
- Retries: max 1 for transient upstream failures
- Circuit breaker opens on sustained error/timeout threshold

Protections:

- Strict response schema validation
- Token budget precheck
- Prompt inputs treat Telegram content as untrusted text
- Authentication goes through Astrixa bearer token or agent-scoped auth
- Guardrails and anonymization are delegated to Astrixa control plane

Fallbacks:

- parsing -> rule-based extractor or manual review
- generation -> template mode
- classification -> `unknown` and manual review

## Security

- Secrets loaded only from runtime config
- No raw secrets in DB or logs
- Telegram content treated as untrusted input
