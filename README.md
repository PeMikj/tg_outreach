# Autonomous Telegram Career Outreach Agent

PoC-система для Telegram outreach с обязательным использованием
[`Astrixa`](./vendor/astrixa) как единственного LLM control plane.

## Состав

- `vendor/astrixa`
  LLM gateway, routing, guardrails, auth, provider abstraction, observability
- `outreach-api`
  HTTP API, operator console, read models, state transitions
- `outreach-worker`
  DB-backed background execution: reply polling, follow-up jobs, recovery loop
- `SQLite`
  локальное хранилище состояния, audit trail, job queue

Внутренний execution flow реализован как явный multi-agent pipeline:

- `Ingestion Agent`
- `Matching & Decision Agent`
- `Generation Agent`
- `Execution & Safety Agent`

Все LLM-dependent шаги выполняются только через `Astrixa`.

## Запуск

1. Подготовить `.env`:

```bash
cp .env.example .env
```

2. Поднять `Astrixa`:

```bash
make astrixa-up
```

3. Поднять PoC:

```bash
make poc-up
```

4. Проверить доступность:

```bash
curl -sS http://127.0.0.1:18080/healthz
curl -sS http://127.0.0.1:18100/healthz
```

5. Открыть operator console:

```text
http://127.0.0.1:18100/ui
```

## Краткая демонстрация

1. Выполнить demo-ingest:

```bash
make demo
```

2. Открыть `http://127.0.0.1:18100/ui`.

3. Проверить:

- появление вакансии в `Review Board`
- `Ops Summary`
- переходы `approve -> queue send -> dispatch` в режиме `dry_run`

## Основные endpoints

- `GET /healthz`
- `GET /metrics`
- `GET /ui`
- `GET /api/v1/config`
- `GET /api/v1/control/emergency-stop`
- `POST /api/v1/control/emergency-stop`
- `POST /api/v1/vacancies/ingest`
- `GET /api/v1/vacancies`
- `GET /api/v1/conversations`
- `GET /api/v1/conversations/{conversation_id}/timeline`
- `GET /api/v1/recruiters`
- `GET /api/v1/recruiters/{recruiter_handle}/overview`
- `POST /api/v1/conversations/reply`
- `GET /api/v1/jobs`
- `GET /api/v1/ops/summary`
- `GET /api/v1/ops/failed-jobs`
- `POST /api/v1/telegram/replies/poll`
- `POST /api/v1/admin/seed-worker-jobs`
- `POST /api/v1/vacancies/{vacancy_id}/approve`
- `POST /api/v1/vacancies/{vacancy_id}/reject`
- `POST /api/v1/vacancies/{vacancy_id}/edit`
- `POST /api/v1/vacancies/{vacancy_id}/queue-send`
- `POST /api/v1/vacancies/{vacancy_id}/dispatch`
- `POST /api/v1/telegram/ingest`

## Текущее поведение

- `dispatch` по умолчанию работает в режиме `dry_run`
- режим `manual_send` существует, но не требуется для демонстрации PoC
- если в вакансии найден email, preferred contact channel = `email`
- если email нет, но найден `@handle` / `t.me/...`, preferred contact channel = `telegram`
- `POST /api/v1/vacancies/ingest` возвращает batch-результат:
  `input_chunks`, `created_count`, `duplicate_count`, `created`
- каждая vacancy содержит:
  `context_bundle`, `approval_expires_at`,
  `structured_data.contact_extraction_status`,
  `structured_data.contact_extraction_reason`
- `dispatch` создает:
  `conversation`, `outreach_attempt`, `follow_up_due` job
- входящие replies поддерживаются через:
  `POST /api/v1/conversations/reply`
  и periodic `telegram_reply_poll` job
- для `manual_send` по email требуется SMTP-конфигурация:
  `TG_OUTREACH_SMTP_HOST`, `TG_OUTREACH_SMTP_PORT`, `TG_OUTREACH_SMTP_USERNAME`,
  `TG_OUTREACH_SMTP_PASSWORD`, `TG_OUTREACH_SMTP_FROM_EMAIL`

## Observability

- `GET /api/v1/ops/summary`
  queue status, failed jobs count, recruiter/conversation state, contact extraction health
- `GET /api/v1/ops/failed-jobs`
  recent failed jobs with attempts and last error

Operator console раздается напрямую из `outreach-api` по `GET /ui`.
Отдельный frontend service и отдельный JS build step не используются.

## Replay / Eval

Для regression replay по сохраненному корпусу вакансий:

```bash
make eval-replay
```

Команда выполняет `python -m app.replay_eval` внутри `outreach-api` и выводит:

- `scanned`
- `changed`
- `unchanged`
- `field_change_counts`
- sample diffs между сохраненным и текущим parser/policy output

## Telegram Dry-Run Ingest

Для чтения реальных Telegram-каналов используется read-only ingest через user session string.

Требуемые переменные:

- `TG_OUTREACH_TELEGRAM_API_ID`
- `TG_OUTREACH_TELEGRAM_API_HASH`
- `TG_OUTREACH_TELEGRAM_SESSION_STRING`
- `TG_OUTREACH_TELEGRAM_CHANNELS`
- `TG_OUTREACH_TELEGRAM_REPLY_POLL_INTERVAL_SECONDS`

Пример запуска:

```bash
curl -sS -X POST http://127.0.0.1:18100/api/v1/telegram/ingest \
  -H 'content-type: application/json' \
  -d '{"per_channel_limit":5}'
```

Этот endpoint только читает сообщения из каналов и создает локальные vacancy records.

## Notifications

Уведомления оператору направляются только в отдельный control target, а не в диалоги с рекрутерами.
