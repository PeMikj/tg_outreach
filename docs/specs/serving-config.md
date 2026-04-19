# Serving and Config Spec

## Runtime Shape

PoC запускается как набор локальных сервисов:

- `vendor/astrixa/*` stack
- `outreach-api`
- `outreach-worker`
- `postgres` service and local volume

Operator console в текущем PoC раздается напрямую из `outreach-api`.

Retriever реализован как библиотечный слой внутри `outreach-api` / `outreach-worker`.

## Configuration Domains

- Telegram credentials/session
- SMTP credentials
- Astrixa base URL/token/model
- upstream provider/model/version inside Astrixa
- retry and timeout budgets
- rate limits and policy thresholds
- local DB path
- observability endpoints

## Config Rules

- Все runtime параметры должны быть заданы явно через env/config file
- Критичный runtime-конфиг должен быть проверяем через `GET /api/v1/config`
- Невалидный конфиг для live path должен блокировать соответствующий side effect
- Значения по умолчанию допустимы только для non-secret параметров

## Secret Handling

- Secrets only via local env file or secret store
- Secrets are never committed
- Secret values are redacted in logs and diagnostics

## Versioning

- Model versions are pinned in config
- Prompt/template versions recorded in execution metadata

## Health and Startup

- Liveness: process alive and event loop responsive
- Readiness for `outreach-api`: DB reachable, schema initialized
- Readiness for `Astrixa`: gateway health endpoint отвечает
- Worker should not execute due jobs until DB is reachable

Текущие operational endpoints:

- `GET /healthz`
- `GET /readyz`
- `GET /api/v1/config`
