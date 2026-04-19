# Data Flow

```mermaid
flowchart LR
    tg[Telegram]
    smtp[SMTP]
    astrixa[Astrixa]
    ui[Operator UI]

    raw[(Raw Events)]
    domain[(Domain State)]
    memory[(Memory Documents)]
    queue[(Job Queue)]
    audit[(Audit Log)]
    metrics[(Metrics / Ops Views)]

    tg -->|channel posts, replies| raw
    raw --> domain
    domain --> queue
    memory -->|retrieved context| astrixa
    domain -->|structured vacancy + summaries| astrixa
    astrixa -->|draft, summary, labels| domain
    domain --> memory
    ui -->|approve, reject, edit, pause| domain
    domain -->|dispatch target| tg
    domain -->|dispatch target| smtp
    domain --> audit
    domain --> metrics
    queue --> metrics
    astrixa --> metrics
    tg --> metrics
```

Диаграмма показывает, какие данные считаются source-of-truth, что индексируется,
а что уходит в логирование и telemetry.
