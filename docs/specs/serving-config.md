# Serving and Config Spec

## Runtime Shape

PoC запускается как набор локальных сервисов:

- `frontend`
- `backend-api`
- `orchestrator-worker`
- `scheduler`
- `postgres`
- `observability`

Retriever может быть отдельным процессом или библиотекой внутри worker на первом этапе.

## Configuration Domains

- Telegram credentials/session
- LLM provider/model/version
- Embedding provider/model/version
- retry and timeout budgets
- rate limits and policy thresholds
- storage encryption settings
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
- Readiness: DB reachable, migrations applied, essential adapters initialized
- Worker should not accept jobs until readiness passes
