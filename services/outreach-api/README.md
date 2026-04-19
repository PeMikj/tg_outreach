# Outreach API

Доменный backend PoC для Telegram career outreach.

Роль сервиса:

- принимать вакансии;
- выполнять parsing, matching, generation и state transitions;
- вызывать `Astrixa` как обязательный LLM control plane;
- хранить состояние, jobs, memory и audit trail;
- отдавать API и встроенный operator UI;
- работать совместно с `outreach-worker`.

Сервис не заменяет `Astrixa`, а использует его как обязательный LLM gateway/control plane.

## Локальный запуск

Полный запуск описан в корневом [README](/home/p/tg_outreach/README.md).

Кратко:

```bash
git submodule update --init --recursive
cp .env.example .env
make astrixa-up
make poc-up
```

Проверка:

```bash
curl -sS http://127.0.0.1:18100/healthz
curl -sS http://127.0.0.1:18100/readyz
curl -sS http://127.0.0.1:18100/api/v1/config
curl -sS http://127.0.0.1:18100/version
curl -sS http://127.0.0.1:18100/api/v1/admin/runtime
```

## Replay / Eval

Для regression replay по уже сохраненным вакансиям:

```bash
python -m app.replay_eval
```

Скрипт перечитывает `vacancies` из текущей primary БД, повторно запускает parser + policy
и показывает, что изменилось относительно сохраненных результатов.
