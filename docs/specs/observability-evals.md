# Observability and Evals Spec

## Purpose

Фиксирует минимальный эксплуатационный baseline для надежного PoC:
что логировать, какие метрики собирать, на что алертить и какие проверки качества запускать.

## Logs

- Structured JSON logs
- Correlation id, job id, conversation id, vacancy id
- External call outcome, timeout, retry and fallback decisions
- PII masking enabled by default

## Metrics

- queue depth
- queue age
- job success/failure rate
- step latency p50/p95/p99
- Telegram send success rate
- Telegram rate-limit event count
- LLM timeout/error rate
- circuit breaker open duration
- retrieval degradation ratio
- approval conversion rate

## Traces

- One trace per workflow execution
- Child spans for DB, Telegram, LLM, retrieval, policy evaluation
- Error tags and retry annotations required

## Alerts

- DB unavailable
- worker backlog above threshold
- repeated send failures
- Telegram flood-wait/cooldown active
- LLM circuit breaker open beyond threshold
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
- external dependency health
- policy denials and manual review volume
