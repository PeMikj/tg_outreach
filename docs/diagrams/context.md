# C4 Context

```mermaid
flowchart LR
    user[User / Operator]
    system[Telegram Career Outreach Agent PoC]
    telegram[Telegram API / MTProto]
    astrixa[Astrixa Gateway]
    smtp[SMTP Server]
    storage[(Local Persistent Storage)]
    obs[Metrics / Audit / Ops Views]

    user -->|approve, reject, pause, inspect| system
    system -->|read channels, send telegram messages, poll replies| telegram
    system -->|send email when enabled| smtp
    system -->|parse, classify, summarize, generate drafts| astrixa
    system -->|state, audit, memory, queue| storage
    system -->|metrics, audit, ops read models| obs
```

Диаграмма показывает внешние границы системы и то, что критичные side effects идут только
через контролируемые интеграционные адаптеры.
