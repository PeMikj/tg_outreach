# Product Proposal

## 1. Обоснование идеи

Задача: построить локальный PoC для Telegram career outreach, который сокращает ручную
обработку вакансий, но не переводит принятие рискованных решений в полностью автономный режим.

Прикладная проблема:

- вакансии публикуются в Telegram как неструктурированный поток;
- кандидат тратит время на ручной просмотр, фильтрацию и повторяющиеся отклики;
- follow-up выполняется нерегулярно;
- растет риск дубликатов, лишних сообщений и ошибок в выборе контакта.

Целевая аудитория PoC:

- индивидуальный кандидат или оператор, который ведет персональный outreach;
- не рекрутинговое агентство и не multi-tenant SaaS.

Формулировка ценности PoC:

- автоматизировать ingest, parsing, ranking и подготовку draft;
- оставить человеческий контроль на approval и side effects;
- снизить операционные риски за счет state machine, идемпотентности и policy enforcement.

## 2. Цель проекта

Цель PoC: показать, что Telegram outreach можно организовать как контролируемый
multi-agent workflow с обязательным использованием `Astrixa` как единственного LLM control plane.

PoC должен продемонстрировать:

- ingest вакансий из Telegram;
- structured parsing и split digest-постов;
- matching и policy decision;
- draft generation и follow-up generation через `Astrixa`;
- operator approval;
- dispatch pipeline в безопасном `dry_run` режиме;
- conversation state и reply handling;
- эксплуатационный baseline: jobs, health, metrics, replay/eval.

## 3. Метрики успеха

### Продуктовые метрики

- precision релевантных вакансий на ручной валидации оператора: `>= 0.75`;
- доля вакансий со статусом `awaiting_approval` среди прошедших hard filters: `>= 0.50`;
- доля draft, одобренных оператором без ручного редактирования: `>= 0.30`;
- время от ingest до review-ready draft: заметно меньше ручного процесса.

### Агентные метрики

- `0` duplicate dispatch на один `contact_target`;
- `0` follow-up после явного rejection;
- не более `1` follow-up на одного рекрутера в рамках conversation;
- `100%` policy enforcement на approval TTL, emergency stop, duplicate guard и daily limits.

### Технические метрики

- `p95 ingest -> draft ready < 15s`;
- `p95 generation step < 6s`;
- `>= 99%` успешных non-side-effect jobs;
- replay/eval контур способен воспроизводимо находить parser/policy drift.

## 4. Сценарии использования

### Основной сценарий

1. Система читает Telegram-канал или получает vacancy через API.
2. `Ingestion Agent` выделяет структурированные поля и контакт.
3. `Matching & Decision Agent` применяет hard filters и вычисляет relevance score.
4. `Generation Agent` собирает context bundle и генерирует draft через `Astrixa`.
5. Оператор просматривает результат в UI.
6. После `approve` формируется dispatch в `dry_run` или `manual_send`.
7. При отсутствии ответа worker планирует follow-up draft.

### Edge-кейсы

- в вакансии нет контакта;
- указан `email`, но нет `telegram handle`;
- указан `telegram handle`, но нет `email`;
- пост является digest и содержит несколько вакансий;
- Telegram выдает flood-wait или временно недоступен;
- LLM timeout или пустой ответ;
- recruiter reply меняет состояние conversation до момента follow-up;
- approval истек до dispatch.

## 5. Ограничения

### Технические

- single-node deployment;
- local `Postgres`, без distributed queue;
- зависимость от Telegram user session для реального ingest;
- зависимость от `Astrixa` и доступного upstream provider для LLM-path;
- ограниченная точность rule-based parsing на шумных Telegram-постах.

### Операционные

- `dry_run` остается режимом по умолчанию;
- live send не обязателен для демонстрации PoC;
- не более `3` initial outreach в день;
- не более `1` follow-up на conversation;
- human approval обязателен перед first send и follow-up send;
- данные хранятся локально на машине оператора.

### Предварительные SLO / budget bounds

- `p95 approval -> dispatch completion < 4s` для `dry_run`;
- poll interval `30-300s` в зависимости от режима;
- ориентир по внешним LLM-затратам: `< $3/day` при нормальной нагрузке PoC.

## 6. Архитектурный набросок

Фактические модули PoC:

1. `Astrixa`
   единый LLM gateway, routing, auth, guardrails, provider abstraction
2. `outreach-api`
   API, operator console, domain state transitions, read models
3. `outreach-worker`
   фоновая обработка jobs, reply poll, follow-up scheduling
4. `Postgres`
   storage для vacancies, conversations, jobs, memory, audit
5. Multi-agent runtime:
   - `Ingestion Agent`
   - `Matching & Decision Agent`
   - `Generation Agent`
   - `Execution & Safety Agent`

## 7. Потенциальный data flow

Основной путь:

`Telegram/API input -> Ingestion Agent -> Matching & Decision Agent -> Generation Agent -> operator approval -> Execution & Safety Agent -> dispatch result -> conversation update -> delayed follow-up evaluation`

Что делегируется LLM через `Astrixa`:

- draft generation;
- follow-up generation;
- reply classification;
- conversation summarization;
- при необходимости LLM-assisted parsing path.

Что не делегируется LLM:

- policy enforcement;
- daily limits;
- duplicate-send guard;
- job scheduling;
- idempotency;
- emergency stop;
- final dispatch decision.

## 8. Почему это соответствует инфраструктурному треку

Основной акцент PoC не на автономности любой ценой, а на устойчивом runtime:

- явная state machine;
- DB-backed job queue;
- degraded modes при проблемах с Telegram или LLM;
- operator approval как обязательная контрольная точка;
- replay/eval и ops summary для контроля качества и отказов.
