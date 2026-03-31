# System Design

## 1. Scope and Design Goals

Этот документ фиксирует архитектуру PoC-системы `Autonomous Telegram Career Outreach Agent`
на уровне, достаточном для начала реализации без существенных архитектурных пробелов.

Фокус данного дизайна: инфраструктурная стабильность, надежность, предсказуемая деградация,
контроль ресурсов и наблюдаемость. LLM остается важным компонентом, но система не должна
становиться недоступной или небезопасной из-за частичной деградации LLM/API.

### In scope

- Мониторинг выбранных Telegram-каналов и входящих диалогов
- Парсинг вакансий и извлечение структурированных атрибутов
- Оценка релевантности вакансии профилю пользователя
- Генерация outreach draft и follow-up draft
- Подтверждение пользователя перед отправкой
- Отправка сообщения через Telegram user client
- Обновление состояния диалога и планирование follow-up
- Структурированные логи, метрики, трейсы, audit trail

### Out of scope

- Мультиарендность
- Высоконагруженный distributed deployment
- Полностью автономная многошаговая переписка без human approval
- Массовая рассылка

## 2. Architectural Principles

### P1. Reliability over autonomy

Критические решения и side effects не должны зависеть только от ответа LLM.
LLM используется как вспомогательный компонент для извлечения структуры и генерации текста,
но не как источник truth для rate limits, policy enforcement и execution control.

### P2. Single-node, durable-by-default PoC

Для PoC выбирается развертывание на одной локальной машине или одном VM-host:

- `api/frontend` для UI и operator actions
- `orchestrator-worker` для фонового исполнения
- `postgres` как основной persistent store
- `object/files` для encrypted artifacts
- `otel/prometheus/grafana` для observability

Это уменьшает операционную сложность, но сохраняет надежные границы между execution,
storage и integrations.

### P3. Explicit state machine

Каждая вакансия, outreach attempt и conversation имеют явное состояние и допустимые переходы.
Это снижает риск повторных отправок, "зависших" задач и неидемпотентного поведения.

### P4. Degrade, do not guess

При недоступности LLM, Telegram API, retrieval или reranking система:

- не выполняет unsafe action;
- переводит задачу в `blocked`, `manual_review` или `retry_pending`;
- использует deterministic fallback только там, где это безопасно.

### P5. Observability is part of the runtime

Каждый шаг выполнения, внешний вызов, retry, timeout, отказ policy и user confirmation
фиксируются в логах/метриках/трейсах. Без этого PoC нельзя считать эксплуатационно готовым.

## 3. Key Architectural Decisions

| Decision | Choice | Why |
|---|---|---|
| Runtime topology | Single-node containers | Достаточно для PoC, проще эксплуатация, меньше moving parts |
| Primary database | PostgreSQL | Надежные транзакции, блокировки, outbox, удобный audit trail |
| Queue model | DB-backed job queue | Не нужен отдельный broker, меньше operational overhead |
| Retrieval | Hybrid: metadata + lexical + optional embeddings/rerank | Работает даже при деградации embedding/LLM-сервисов |
| LLM access | External API behind adapter + circuit breaker | Изоляция провайдера, retries, fallback, timeout control |
| Telegram integration | User client adapter with rate limiter and idempotency checks | Для реальной отправки сообщений рекрутерам нужен пользовательский клиент |
| Secrets | Local secret store / env injection, never in DB logs | Минимизация утечек |
| Approval model | Human approval before send and follow-up | Контроль рисков, уменьшение false positives |

## 4. System Context

Система является локальным операторским инструментом для одного пользователя.

Внешние зависимости:

- Telegram API / MTProto для чтения каналов и отправки сообщений
- LLM API для parsing/classification/draft generation
- Embedding API или локальная embedding model, если включен semantic retrieval
- Operator UI, через который пользователь подтверждает действия и видит alerts

## 5. Modules and Responsibilities

### 5.1 Frontend / Operator Console

- Показывает новые вакансии, match score, draft, risk flags
- Принимает `approve`, `reject`, `edit`, `pause`, `resume`, `emergency_stop`
- Показывает job state, retries, last failures, API health

### 5.2 API / Backend Gateway

- Принимает UI-команды и отдает read models
- Валидирует входные данные
- Записывает operator actions в БД
- Публикует jobs в execution queue

### 5.3 Orchestrator

- Исполняет workflow как state machine
- Запускает шаги parsing, retrieval, scoring, policy, draft generation, approval wait,
  send, conversation update, follow-up scheduling
- Управляет retry/fallback/circuit breaker outcomes
- Гарантирует идемпотентность шагов

### 5.4 Policy & Guardrail Engine

- Проверяет hard rules:
  - daily outreach limit
  - no duplicate outreach
  - stop after rejection
  - max one follow-up
  - confirmation required
- Выдает `allow`, `deny`, `manual_review`

### 5.5 Retriever

- Индексирует профиль пользователя, CV, шаблоны, прошлые outreach, summaries диалогов
- Выполняет поиск контекста для scoring/generation
- Делает metadata filtering, lexical search, optional semantic rerank

### 5.6 Tool / Integration Layer

- Telegram client adapter
- LLM adapter
- Embedding adapter
- Clock/scheduler
- Encryption/file storage adapter

Каждый адаптер имеет единый контракт ошибок, timeout, retry budget и telemetry hooks.

### 5.7 State / Memory Store

- Хранит вакансии, recruiter entities, outreach attempts, conversations, pending jobs,
  approval state, rate-limit counters, summaries и audit events
- Поддерживает optimistic locking или row-level locking для безопасного исполнения

### 5.8 Scheduler

- Запускает poll jobs
- Ресканит pending/retry jobs
- Инициирует delayed follow-up evaluation
- Выполняет recovery после рестарта

### 5.9 Observability Stack

- Structured logs
- Metrics/alerts
- Distributed traces
- Audit log
- Dead-letter / failed-jobs dashboard

## 6. Execution Flow

### 6.1 Main request flow: new vacancy to approved send

1. `Telegram Poller` читает новые сообщения из выбранных каналов.
2. Raw message сохраняется как immutable event.
3. Orchestrator создает `vacancy_ingest` job.
4. Parser извлекает структуру вакансии.
5. Retriever достает релевантный пользовательский контекст.
6. Scoring step считает match score.
7. Policy engine проверяет, можно ли продолжать обработку.
8. Draft generation строит outreach draft.
9. Draft и risk flags публикуются в UI со статусом `awaiting_approval`.
10. Пользователь делает `approve`, `reject` или `edit`.
11. Если approval получен, orchestrator повторно выполняет policy check перед side effect.
12. Telegram adapter отправляет сообщение с idempotency guard.
13. Conversation state обновляется, создается follow-up timer.
14. Execution result, метрики и audit events записываются в observability stack.

### 6.2 Incoming recruiter response flow

1. Poller получает входящее сообщение.
2. Система определяет conversation и сохраняет raw event.
3. Classifier отмечает: `positive`, `negative`, `needs_manual_reply`, `irrelevant`, `unknown`.
4. Policy engine обновляет conversation status.
5. Если это rejection, все pending follow-up jobs отменяются.
6. Если требуется ответ, UI показывает draft suggestion, но отправка автоматом не выполняется.

### 6.3 Follow-up flow

1. Scheduler находит outreach без ответа по истечении delay window.
2. Policy engine проверяет, не было ли отказа, follow-up ранее, дневной лимит и cooldown.
3. Генерируется follow-up draft или создается manual review, если LLM unavailable.
4. Пользователь подтверждает отправку.
5. После отправки conversation state обновляется, новые авто-follow-up не планируются.

## 7. State, Memory and Context Handling

### 7.1 Core entities

- `telegram_message_raw`
- `vacancy`
- `recruiter`
- `conversation`
- `outreach_attempt`
- `approval_request`
- `job_execution`
- `policy_decision`
- `memory_document`
- `conversation_summary`
- `audit_event`

### 7.2 Memory policy

Память делится на несколько слоев:

- `Session state`: текущее состояние workflow/job execution
- `Operational state`: counters, limits, timers, retry state
- `Long-term memory`: CV, profile, user preferences, past approved outreach, conversation summaries
- `Audit memory`: кто, когда и почему инициировал действие

### 7.3 Context assembly

Context builder формирует prompt/input из:

- краткого структурированного описания вакансии;
- top-k релевантных profile snippets;
- последних summary по recruiter/conversation;
- policy annotations и hard constraints;
- template fragments.

### 7.4 Context budget

- Не передавать полный CV или полный чат, если достаточно summary/snippets
- Сначала использовать structured fields и summaries
- Full raw content добавлять только по explicit need
- Token budget контролируется preflight estimator
- При превышении лимита применяется progressive truncation:
  1. drop low-score memory chunks
  2. compress conversation history to summary
  3. switch to template fallback

## 8. Retrieval Contour

### Sources

- User CV / profile
- User preferences and constraints
- Historical approved outreach messages
- Historical recruiter outcomes
- Conversation summaries
- Vacancy corpus

### Indexing

- Structured metadata in Postgres
- Lexical index for fast keyword search
- Optional vector embeddings for semantic recall

### Query path

1. Metadata filter by source type, recruiter, role family, language.
2. Lexical candidate retrieval.
3. Optional semantic rerank if embeddings available.
4. Top-k cut with score threshold.
5. Result validation to avoid stale/oversized context.

### Retrieval fallback

- Если embeddings unavailable, используем lexical-only retrieval.
- Если reranker unavailable, возвращаем deterministic top-k lexical candidates.
- Если retrieval вернул мало контекста, generation step переключается на conservative template mode.

## 9. Tool and API Integrations

### Telegram adapter

- Operations:
  - poll channel messages
  - poll direct replies
  - send message
  - read message metadata
- Guarantees:
  - rate limit awareness
  - idempotency by conversation/recruiter/outreach key
  - cooldown after flood warning

### LLM adapter

- Operations:
  - vacancy parsing
  - message generation
  - reply classification
  - optional summarization
- Reliability policy:
  - bounded timeout
  - capped retries
  - circuit breaker
  - provider health state in memory
  - fallback to deterministic parser/template where possible

### Embedding adapter

- Operation:
  - create embeddings for memory documents
- Reliability policy:
  - asynchronous indexing
  - batch mode
  - lexical-only retrieval when unavailable

## 10. Failure Modes, Fallbacks and Guardrails

| Failure mode | Detection | System action | User impact |
|---|---|---|---|
| LLM timeout / 5xx | timeout, error rate, circuit breaker | retry, then fallback to template/manual review | slower throughput, but no unsafe send |
| Telegram flood wait / rate limit | API error code | cooldown, reschedule jobs, raise alert | delayed send |
| Duplicate execution after restart | duplicate job key | idempotency check blocks side effect | no duplicate send |
| DB unavailable | health check / failed transaction | stop workers, keep UI in degraded read-only mode | temporary pause |
| Retrieval degraded | health metrics / adapter failure | lexical-only mode | lower quality draft |
| Prompt injection in vacancy/chat | parser validation / policy | strip unsafe instructions, keep raw as untrusted content | no command execution risk |
| Context overflow | token estimate | truncate/summarize/template fallback | lower personalization |
| Human approval stale | TTL expired | invalidate approval and require re-approval | extra operator step |

### Guardrails

- Hard deny on duplicate recruiter outreach
- Hard deny after rejection
- Hard deny above daily outreach limit
- Hard deny when Telegram adapter in cooldown
- Hard deny when approval missing or expired
- No automatic send from raw LLM output without validation
- Mask PII in logs
- Emergency stop blocks all send jobs immediately

## 11. Technical and Operational Constraints

### Latency targets

- `p95 vacancy ingest to draft ready` < 15 s
- `p95 draft generation step` < 6 s
- `p95 approval to send completion` < 4 s
- `poll cycle interval` 30-60 s

### Cost targets

- PoC target: < $3/day average external API spend under normal use
- Aggressive caching for parsing results, embeddings and repeated recruiter context
- Limit LLM calls per vacancy:
  - 1 parsing call max
  - 1 draft generation call max
  - 1 classification call per incoming recruiter message max

### Reliability targets

- No duplicate send events
- Successful recovery of pending jobs after process restart
- `>= 99%` successful completion for non-side-effect internal jobs
- `>= 95%` successful send attempts excluding upstream Telegram outages

### Resource utilization

- Single-node deployment on 2-4 vCPU, 8 GB RAM should be sufficient
- Background workers use bounded concurrency
- Embedding/indexing jobs are throttled and pausable
- Backpressure is applied when queue grows or upstream errors spike

## 12. Control Points

Контрольные точки обязательны перед переходом к реализации:

1. Подтвержден единый state model и список статусов jobs/conversations/outreach.
2. Зафиксированы contracts для Telegram, LLM и retrieval adapters.
3. Зафиксированы идемпотентные ключи для side effects.
4. Подтверждены retry policy, timeout budget и circuit breaker thresholds.
5. Определен минимальный observability baseline: logs, metrics, traces, alerts.
6. Подтвержден набор operator controls: approve, reject, pause, emergency stop, retry.

## 13. Implementation Readiness

Архитектура готова к реализации, если первый инкремент включает:

- storage schema и state machine;
- orchestrator с DB-backed queue;
- Telegram adapter с read/send и cooldown handling;
- LLM adapter с timeout, retry, circuit breaker;
- basic retriever с metadata + lexical retrieval;
- operator console/API для approval;
- observability baseline и alerting.

Без этих элементов PoC нельзя считать надежным даже при корректной бизнес-логике.
