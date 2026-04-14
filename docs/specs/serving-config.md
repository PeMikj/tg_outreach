# Serving and Config Spec

## Runtime Shape

PoC запускается как набор локальных сервисов:

- `vendor/astrixa/*` stack
- `outreach-api`
- `outreach-worker`
- `sqlite` file in local volume

Operator console в текущем PoC раздается напрямую из `outreach-api`.

Retriever реализован как библиотечный слой внутри `outreach-api` / `outreach-worker`.

## Configuration Domains

- Telegram credentials/session
- Astrixa base URL/token/model
- upstream provider/model/version inside Astrixa
- retry and timeout budgets
- rate limits and policy thresholds
- local DB path
- observability endpoints

## Config Rules

- Все runtime параметры должны быть заданы явно через env/config file
- Конфиг валидируется на старте
- Невалидный конфиг должен блокировать запуск
- Значения по умолчанию допустимы только для non-secret параметров

## Secret Handling

- Secrets only via local env file or secret store
- Secrets are never committed
- Secret values are redacted in logs and diagnostics

## Versioning

- Model versions are pinned in config
- Schema migrations versioned
- Prompt/template versions recorded in execution metadata

## Health and Startup

- Liveness: process alive and event loop responsive
- Readiness: DB reachable, schema initialized, essential adapters initialized
- Worker should not accept jobs until readiness passes
