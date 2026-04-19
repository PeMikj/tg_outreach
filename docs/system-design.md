# System Design

## 1. Scope and Design Goals

Этот документ фиксирует архитектуру PoC-системы `Autonomous Telegram Career Outreach Agent`
на уровне, достаточном для начала реализации без существенных архитектурных пробелов.

Фокус данного дизайна: инфраструктурная стабильность, надежность, предсказуемая деградация,
контроль ресурсов и наблюдаемость. LLM остается важным компонентом, но система не должна
становиться недоступной или небезопасной из-за частичной деградации LLM/API.

Для реализации PoC в качестве обязательного LLM control plane используется `Astrixa`
([vendor/astrixa](/home/p/tg_outreach/vendor/astrixa)).

Архитектурное ограничение: никакие агентные шаги не должны обращаться к внешним LLM API
напрямую. Все LLM-dependent операции выполняются только через `Astrixa`.

### In scope

- Мониторинг выбранных Telegram-каналов и входящих диалогов
- Парсинг вакансий и извлечение структурированных атрибутов
- Оценка релевантности вакансии профилю пользователя
- Генерация outreach draft и follow-up draft
- Подтверждение пользователя перед отправкой
- Отправка сообщения через Telegram user client или SMTP email adapter
- Обновление состояния диалога и планирование follow-up
- Метрики, audit trail, ops/read models, replay/eval baseline

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

- `outreach-api` для API, read models и встроенного operator console
- `outreach-worker` для фонового исполнения
- `astrixa gateway stack` для LLM routing, guardrails, auth and observability
- `postgres` как primary persistent store и DB-backed queue
- `operator console` раздается напрямую из `outreach-api`

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
должны быть видимы хотя бы через metrics, audit events и read models. Полноценный
distributed tracing для текущего PoC не является обязательным runtime-компонентом.

## 3. Key Architectural Decisions

| Decision | Choice | Why |
|---|---|---|
| Runtime topology | Single-node containers | Достаточно для PoC, проще эксплуатация, меньше moving parts |
| Primary database | Postgres | Более взрослый operational baseline, лучшее concurrency behavior и явная server-side durability |
| Queue model | DB-backed job queue | Не нужен отдельный broker, меньше operational overhead |
| Retrieval | Retrieval-lite: metadata + in-process lexical/context assembly | Работает без отдельного vector store и без внешнего retriever service |
| LLM access | Astrixa gateway + domain adapter | Поверх провайдеров появляется единый control plane с routing, guardrails, auth и telemetry |
| Contact routing | Email first, then Telegram handle | В реальных постах часто встречается email без Telegram handle |
| Telegram integration | User client adapter with rate limiter and idempotency checks | Для реальной отправки сообщений рекрутерам нужен пользовательский клиент |
| Secrets | Local secret store / env injection, never in DB logs | Минимизация утечек |
| Approval model | Human approval before send and follow-up | Контроль рисков, уменьшение false positives |

## 4. System Context

Система является локальным операторским инструментом для одного пользователя.

Внешние зависимости:

- Telegram API / MTProto для чтения каналов и отправки сообщений
- SMTP server для live email send, если `manual_send` включен
- Astrixa Gateway для parsing/classification/draft generation
- LLM API провайдеров, скрытые за Astrixa
- Operator UI, через который пользователь подтверждает действия и видит alerts

## 5. Modules and Responsibilities

### 5.1 Frontend / Operator Console

- Реализован как встроенный static UI внутри `outreach-api`
- Показывает новые вакансии, match score, draft, risk flags
- Принимает `approve`, `reject`, `edit`, `pause`, `resume`, `emergency_stop`
- Показывает job state, retries, last failures, API health

### 5.2 API / Backend Gateway

- Принимает UI-команды и отдает read models
- Валидирует входные данные
- Записывает operator actions в БД
- Публикует jobs в execution queue

### 5.3 Supervisor / Orchestrator

- Исполняет workflow как state machine и координирует handoff между агентами
- Не генерирует контент сам; управляет ролями, переходами, retry и stop conditions
- Управляет retry/fallback/circuit breaker outcomes
- Гарантирует идемпотентность шагов и side effects

### 5.4 Ingestion Agent

- Читает Telegram-каналы и принимает raw events
- Делает первичную фильтрацию сообщений и split digest-постов
- Выполняет parsing в structured vacancy contract
- Извлекает `recruiter_handle`, contact metadata и parsing trace
- Если для parsing/classification включается LLM-assisted path, он идет только через `Astrixa`

### 5.5 Matching & Decision Agent

- Сравнивает structured vacancy с профилем кандидата
- Считает explainable score и policy-aligned decision
- Учитывает hard filters, salary constraints, anti-spam и историю отправок
- Возвращает `allow`, `deny` или `manual_review`

### 5.6 Generation Agent

- Собирает context bundle в рамках token budget
- Генерирует personalized draft только через `Astrixa`
- Применяет post-processing и fallback, если LLM unavailable или ответ unsafe

### 5.7 Execution & Safety Agent

- Делает финальный safety gate перед operator approval и перед send
- Проверяет approval TTL, emergency stop, duplicate-send guard и dispatch limits
- Управляет send/dry-run side effects и audit logging

### 5.8 Policy & Guardrail Engine

- Проверяет hard rules:
  - daily outreach limit
  - no duplicate outreach
  - stop after rejection
  - max one follow-up
  - confirmation required
- Выдает `allow`, `deny`, `manual_review`

### 5.9 Retriever

- В текущем PoC реализован как in-process context builder, а не как отдельный сервис
- Использует профиль пользователя, preferences, approved snippets, recruiter profile и summaries
- Выполняет metadata filtering и context assembly без отдельного vector store

### 5.10 Tool / Integration Layer

- Telegram client adapter
- SMTP email adapter
- Astrixa adapter
- Clock/scheduler
- Local file/config adapter

Каждый адаптер имеет единый контракт ошибок, timeout, retry budget и telemetry hooks.

### 5.11 State / Memory Store

- Хранит вакансии, recruiter entities, outreach attempts, conversations, pending jobs,
  approval state, rate-limit counters, summaries и audit events
- Для текущего PoC реализован на `Postgres`
- Использует транзакции и lease-based job handling для безопасного исполнения

### 5.12 Scheduler

- Запускает poll jobs
- Ресканит pending/retry jobs
- Инициирует delayed follow-up evaluation
- Выполняет recovery после рестарта
- В текущем PoC реализован внутри `outreach-worker`, отдельный process `scheduler` не используется

### 5.13 Observability Stack

- Prometheus metrics из `outreach-api`
- Audit log
- Ops summary / failed-jobs read models
- Astrixa-side observability для LLM control plane

### 5.14 Notification Adapter

- Отправляет operator-facing уведомления в отдельный control channel
- Не используется для outreach к рекрутерам
- Поддерживает deduplication по `event_type + entity_id + status`
- Используется для:
  - new high relevance vacancy
  - awaiting approval
  - ingest degradation
  - provider degraded
  - daily limit reached

## 6. Execution Flow

### 6.1 Main request flow: new vacancy to approved send

1. `Telegram Poller` читает новые сообщения из выбранных каналов.
2. Raw message сохраняется как immutable event.
3. Supervisor создает `vacancy_ingest` execution.
4. `Ingestion Agent` извлекает structured vacancy и contact metadata.
5. `Matching & Decision Agent` считает match score и policy-aligned decision.
6. `Generation Agent` собирает context bundle и строит outreach draft.
7. `Execution & Safety Agent` проверяет approval gate, emergency stop и send preconditions.
8. Draft и risk flags публикуются в UI со статусом `awaiting_approval` или `manual_review`.
9. Notification adapter отправляет оператору краткое уведомление в отдельный control channel.
10. Пользователь делает `approve`, `reject` или `edit`.
11. Если approval получен, `Execution & Safety Agent` повторно валидирует TTL, duplicate-send guard и limits.
12. Execution path выбирает `email` или `telegram` по `preferred_contact_channel`.
13. Adapter отправляет сообщение с idempotency guard или выполняет `dry_run`.
14. Conversation state обновляется там, где это применимо, и создается follow-up timer.
15. Execution result, agent trace, метрики и audit events записываются в storage и read models.

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
- `job_execution`
- `memory_document`
- `conversation_summary`
- `audit_event`
- `control_state`

### 7.2 Memory policy

Память делится на несколько слоев:

- `Session state`: текущее состояние workflow/job execution
- `Operational state`: counters, limits, timers, retry state
- `Long-term memory`: CV, profile, user preferences, past approved outreach, conversation summaries
- `Audit memory`: кто, когда и почему инициировал действие

### 7.3 Context assembly

Context builder формируется `Generation Agent` и выдает `context_bundle`:

- краткого структурированного описания вакансии;
- top-k релевантных profile snippets;
- последних summary по recruiter/conversation;
- recruiter profile;
- approved outreach snippets;
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
- Recruiter profile snippets

### Indexing

- Structured metadata in `Postgres`
- In-process lexical/context assembly
- Отдельный vector index и semantic rerank не являются обязательной частью текущего PoC

### Query path

1. Metadata filter by source type, recruiter, role family, language.
2. Deterministic selection of profile, preferences, recruiter memory and snippets.
3. Budget-aware truncation.
4. Result validation to avoid stale/oversized context.

### Retrieval fallback

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

### SMTP email adapter

- Operations:
  - send email
- Guarantees:
  - explicit target required
  - no live send in `dry_run`
  - audit event and dispatch history on every attempt

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

## 10. Failure Modes, Fallbacks and Guardrails

| Failure mode | Detection | System action | User impact |
|---|---|---|---|
| LLM timeout / 5xx | timeout, error rate, circuit breaker | retry, then fallback to template/manual review | slower throughput, but no unsafe send |
| Telegram flood wait / rate limit | API error code | cooldown, reschedule jobs, raise alert | delayed send |
| Duplicate execution after restart | duplicate job key | idempotency check blocks side effect | no duplicate send |
| DB unavailable | health check / failed transaction | stop workers, keep UI in degraded read-only mode | temporary pause |
| Retrieval degraded | low-context flag / missing memory | template fallback or manual review | lower quality draft |
| Prompt injection in vacancy/chat | parser validation / policy | strip unsafe instructions, keep raw as untrusted content | no command execution risk |
| Context overflow | token estimate | truncate/summarize/template fallback | lower personalization |
| Human approval stale | TTL expired | invalidate approval and require re-approval | extra operator step |
| Missing contact target | contact extraction status | manual review, no send | throughput reduction |

### Guardrails

- Hard deny on duplicate recruiter outreach
- Hard deny after rejection
- Hard deny above daily outreach limit
- Hard deny when Telegram adapter in cooldown
- Hard deny when approval missing or expired
- No automatic send from raw LLM output without validation
- `dry_run` by default
- Emergency stop blocks all send jobs immediately
- Operator notifications go only to dedicated control target, never to recruiter dialogs

## 11. Technical and Operational Constraints

### Latency targets

- `p95 vacancy ingest to draft ready` < 15 s
- `p95 draft generation step` < 6 s
- `p95 approval to send completion` < 4 s
- `poll cycle interval` 30-60 s

### Cost targets

- PoC target: < $3/day average external API spend under normal use
- Aggressive reuse of parsing results and repeated recruiter context
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
- Backpressure is applied when queue grows or upstream errors spike

## 12. Control Points

Контрольные точки обязательны перед переходом к реализации:

1. Подтвержден единый state model и список статусов jobs/conversations/outreach.
2. Зафиксированы contracts для Telegram, LLM и retrieval adapters.
3. Зафиксированы идемпотентные ключи для side effects.
4. Подтверждены retry policy, timeout budget и circuit breaker thresholds.
5. Определен минимальный observability baseline: metrics, audit events, read models, replay checks.
6. Подтвержден набор operator controls: approve, reject, pause, emergency stop, retry.
7. Зафиксирован notification target для operator alerts and approvals.

## 13. Implementation Readiness

Архитектура готова к реализации, если первый инкремент включает:

- storage schema и state machine;
- orchestrator с DB-backed queue;
- Telegram adapter с read/send и cooldown handling;
- SMTP email adapter для contact routing;
- LLM adapter с timeout, retry, circuit breaker;
- basic retriever-lite с metadata + in-process context assembly;
- operator console/API для approval;
- observability baseline и alerting.

Без этих элементов PoC нельзя считать надежным даже при корректной бизнес-логике.
