# Autonomous Telegram Career Outreach Agent

PoC-система для Telegram career outreach: ingest вакансий, parsing, matching,
draft generation, operator approval, controlled dispatch, reply handling и follow-up.

Обязательное архитектурное ограничение: все LLM-dependent шаги выполняются только через
[`Astrixa`](./vendor/astrixa). Прямые вызовы во внешние LLM API из доменной логики запрещены.

## Задача

Система предназначена для одного оператора, который отслеживает вакансии в Telegram
и управляет outreach без ручного просмотра всего потока публикаций.

Текущая боль:

- вакансии публикуются как неструктурированный поток;
- релевантность приходится оценивать вручную;
- outreach и follow-up выполняются непоследовательно;
- легко допустить дубликаты, ошибки в контактах и лишние сообщения.

## Что делает PoC на демо

PoC демонстрирует следующий безопасный контур:

- ingest вакансий из ручного API и из Telegram-каналов;
- parsing и split digest-постов;
- matching и policy decision;
- draft generation через `Astrixa`;
- operator review через встроенный UI;
- approval, queue-send и dispatch в `dry_run`;
- conversation state, reply polling, follow-up draft generation;
- audit trail, jobs, ops summary и replay/eval.

## Что PoC не делает

Явно out of scope:

- мультиарендность;
- production-grade distributed deployment;
- полностью автономную переписку без human approval;
- массовую рассылку;
- обязательную live-send демонстрацию во внешние чаты или email.

## Состав

- `vendor/astrixa`
  LLM gateway, routing, auth, guardrails, provider abstraction, базовая telemetry
- `outreach-api`
  HTTP API, встроенный operator console, read models, state transitions, metrics
- `outreach-worker`
  DB-backed background execution: reply polling, follow-up jobs, recovery loop
- `Postgres`
  primary durable storage для состояния, audit trail и job queue

Внутренний execution flow реализован как явный multi-agent pipeline:

- `Ingestion Agent`
- `Matching & Decision Agent`
- `Generation Agent`
- `Execution & Safety Agent`

## Prerequisites

Требуется:

- `git` c поддержкой submodule;
- `docker`;
- `docker compose`;
- доступ к upstream LLM provider, настроенному в `Astrixa`, либо рабочая mock-конфигурация внутри `Astrixa`.

Для Telegram ingest дополнительно требуются:

- `TG_OUTREACH_TELEGRAM_API_ID`
- `TG_OUTREACH_TELEGRAM_API_HASH`
- `TG_OUTREACH_TELEGRAM_SESSION_STRING`

Для live email send в режиме `manual_send` дополнительно требуются:

- `TG_OUTREACH_SMTP_HOST`
- `TG_OUTREACH_SMTP_PORT`
- `TG_OUTREACH_SMTP_USERNAME`
- `TG_OUTREACH_SMTP_PASSWORD`
- `TG_OUTREACH_SMTP_FROM_EMAIL`

## Запуск

1. Клонировать репозиторий вместе с submodule:

```bash
git clone <repo-url>
cd tg_outreach
git submodule update --init --recursive
```

2. Подготовить `.env`:

```bash
cp .env.example .env
```

3. Заполнить в `.env` как минимум:

- токен `Astrixa`;
- параметры upstream provider или mock-конфигурацию в `Astrixa`;
- Telegram credentials, если нужен реальный channel ingest;
- SMTP credentials только если нужен live email send.

4. Поднять `Astrixa`:

```bash
make astrixa-up
```

5. Поднять PoC:

```bash
make poc-up
```

6. Проверить доступность:

```bash
curl -sS http://127.0.0.1:18080/healthz
curl -sS http://127.0.0.1:18100/healthz
curl -sS http://127.0.0.1:18100/readyz
curl -sS http://127.0.0.1:18100/version
curl -sS http://127.0.0.1:18100/api/v1/admin/runtime
curl -sS http://127.0.0.1:18100/api/v1/admin/dependencies
```

Ожидаемые ответы:

- `{"status":"ok","service":"api-gateway"}`
- `{"status":"ok","service":"tg-outreach-api"}`
- `{"status":"ok","service":"tg-outreach-api","database_backend":"postgres",...}`
- `{"service":"tg-outreach-api","version":"...","git_sha":"...","database_backend":"postgres"}`
- `{"worker":{"status":"ok",...},"secret_status":{"astrixa_token_configured":true,...},...}`
- `{"database":{"status":"ok",...},"astrixa":{"health":{"status":"ok",...},"invoke_probe":{"status":"ok|degraded|error",...}}}`

7. Открыть operator console:

```text
http://127.0.0.1:18100/ui
```

## Минимальная демонстрация

1. Выполнить demo-ingest:

```bash
make demo
```

2. Открыть `http://127.0.0.1:18100/ui`.

3. Проверить:

- появление vacancy в `Review Board`;
- `Ops Summary`;
- переходы `approve -> queue send -> dispatch`;
- конечный статус `sent_dry_run`.

## Как понять, что проект поднялся корректно

Минимальный operational check:

```bash
make health
make status
make smoke
make security-check
make verify
make migrate
make preflight
make test
make cleanup-demo-data
curl -sS http://127.0.0.1:18100/api/v1/config
curl -sS http://127.0.0.1:18100/api/v1/ops/summary
curl -sS http://127.0.0.1:18100/api/v1/jobs
```

Признаки корректного старта:

- API отвечает без `5xx`;
- `readyz` подтверждает `database=ok` и `astrixa=ok`;
- `smoke` создает новую test vacancy и подтверждает `created_count > 0`;
- `security-check` подтверждает, что tracked files не содержат очевидных секретов;
- `verify` прогоняет compile check, secret hygiene check и smoke check;
- `migrate` явно выводит applied SQL migrations;
- `preflight` прогоняет verify, migrate, version, runtime и dependency probes;
- `test` запускает минимальные regression tests для runtime validation, migrate command и `ops/summary`;
- `test` также покрывает contract для `security-check`;
- `cleanup-demo-data` удаляет demo/smoke записи и связанные test artifacts из runtime storage;
- конфиг читается;
- `ops/summary` возвращает агрегаты;
- `ops/summary` показывает `generation_sources` и `fallback_generations` для контроля деградации generation path;
- `ops/summary` показывает `worker_status`, `astrixa_health_status`, `astrixa_invoke_status` и `dependency_degraded`;
- worker создает и обрабатывает фоновые jobs.

Быстрый smoke test:

```bash
make smoke
```

## Основные endpoints

- `GET /healthz`
- `GET /readyz`
- `GET /version`
- `GET /api/v1/admin/runtime`
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

- `dispatch` по умолчанию работает в режиме `dry_run`;
- `manual_send` существует, но не требуется для демонстрации PoC;
- если в вакансии найден `email`, preferred contact channel = `email`;
- если `email` нет, но найден `@handle` или `t.me/...`, preferred contact channel = `telegram`;
- если нет ни `email`, ни `telegram handle`, кейс остается в review path;
- `POST /api/v1/vacancies/ingest` возвращает batch-результат:
  `input_chunks`, `created_count`, `duplicate_count`, `created`;
- каждая vacancy содержит:
  `context_bundle`, `approval_expires_at`,
  `structured_data.contact_extraction_status`,
  `structured_data.contact_extraction_reason`,
  `structured_data.preferred_contact_channel`;
- `dispatch` создает:
  `outreach_attempt`, audit events и при наличии recruiter conversation state;
- входящие replies поддерживаются через:
  `POST /api/v1/conversations/reply`
  и periodic `telegram_reply_poll` job.

## Observability

- `GET /api/v1/ops/summary`
  queue status, failed jobs count, recruiter/conversation state, contact extraction health
- `GET /api/v1/ops/failed-jobs`
  recent failed jobs with attempts and last error
- `GET /metrics`
  Prometheus metrics из `outreach-api`

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
