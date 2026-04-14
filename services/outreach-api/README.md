# Outreach API

Доменный backend PoC для Telegram career outreach.

Роль сервиса:

- принимать вакансии;
- считать базовый match score;
- вызывать `Astrixa` для генерации draft;
- хранить состояние и audit trail;
- отдавать API для manual approval и mock dispatch.

Сервис не заменяет `Astrixa`, а использует его как обязательный LLM gateway/control plane.

Для regression replay по уже сохраненным вакансиям:

```bash
python -m app.replay_eval
```

Скрипт перечитывает `vacancies` из текущей SQLite БД, повторно запускает parser + policy
и показывает, что изменилось относительно сохраненных результатов.
