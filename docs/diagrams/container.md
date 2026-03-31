# C4 Container

```mermaid
flowchart TB
    subgraph User Device
        ui[Frontend / Operator Console]
    end

    subgraph PoC Node
        api[API / Backend Gateway]
        orch[Orchestrator Worker]
        retr[Retriever Service]
        tools[Tool Layer\nTelegram / LLM / Embeddings]
        sched[Scheduler]
        db[(PostgreSQL)]
        files[(Encrypted Files)]
        obs[Observability\nOTel + Prometheus + Grafana]
    end

    tg[Telegram API]
    llm[LLM API]
    emb[Embedding API or Local Model]

    ui --> api
    api --> db
    api --> obs
    api --> orch
    sched --> orch
    orch --> db
    orch --> retr
    orch --> tools
    orch --> obs
    retr --> db
    retr --> tools
    retr --> obs
    db --> files
    tools --> tg
    tools --> llm
    tools --> emb
```

Диаграмма разделяет control plane, execution plane, integrations и persistent state.
