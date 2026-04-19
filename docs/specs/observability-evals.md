# Observability and Evals Spec

## Purpose

Фиксирует минимальный эксплуатационный baseline для надежного PoC:
что логировать, какие метрики собирать, на что алертить и какие проверки качества запускать.

## Logs

- Audit events and runtime diagnostics must remain separate
- Job id, conversation id, vacancy id and entity ids must be recoverable from read models
- External call outcome, timeout, retry and fallback decisions must be observable

Ограничение текущего PoC:

- полноценный structured JSON logging pipeline не является завершенной частью реализации;
- базовый observability layer строится на metrics, audit tables и API read models.

## Metrics

- queue depth
- queue age
- job success/failure rate
- request and step latency
- Telegram send success rate
- Telegram rate-limit event count
- LLM timeout/error rate
- low-context / degraded generation ratio
- approval conversion rate

## Traces

В текущем PoC вместо полноценного tracing используются:

- `agent_trace` внутри `context_bundle`;
- audit timeline;
- per-step metrics.

## Alerts

- DB unavailable
- worker backlog above threshold
- repeated send failures
- Telegram flood-wait/cooldown active
- no successful poll cycle within threshold

## Evals and Operational Checks

- golden tests for parsing and draft validation
- replay tests on stored raw events
- duplicate-send prevention test
- restart recovery test
- rate-limit cooldown test
- approval TTL expiration test

## Dashboards

- system health
- execution pipeline
- Astrixa and Telegram dependency health
- policy denials and manual review volume
- failed jobs and retry visibility
- recruiter/contact extraction health

## Read Models

Минимальный PoC dashboard/read-only слой должен быть доступен через API:

- `GET /api/v1/ops/summary`
  - jobs by status/type
  - due pending jobs
  - overdue leased jobs
  - failed jobs
  - recruiter status distribution
  - conversation status distribution
  - contact extraction status/reason distribution
- `GET /api/v1/ops/failed-jobs`
  - recent failed jobs
  - attempts / max_attempts
  - last error
