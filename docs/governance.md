# Governance

## 1. Risk Register

| Риск | Вероятность / влияние | Детект | Защита | Остаточный риск |
|---|---|---|---|---|
| Duplicate send одному контакту | Средняя / высокое | `dispatch_events`, audit timeline, operator review | contact-level duplicate guard, idempotent dispatch pipeline, explicit state machine | Низкий |
| Follow-up после rejection | Низкая / высокое | conversation status, cancelled jobs | policy deny, follow-up cancellation on inbound reply | Низкий |
| Flood-wait или rate-limit Telegram | Средняя / среднее | send errors, failed jobs, retry state | cooldown, retry budget, `dry_run` default | Средний |
| Недоступность LLM provider или `Astrixa` | Средняя / высокое | failed Astrixa calls, degraded generation | fallback drafts, manual review, no unsafe send | Средний |
| Ошибочное извлечение контакта | Средняя / высокое | `contact_extraction_status`, operator review, timeline | email-first routing, deterministic extraction, manual review path | Средний |
| Prompt injection в тексте вакансии или reply | Средняя / среднее | unsafe output, parser anomalies, manual review | treat Telegram text as untrusted input, no tool execution from raw text, deterministic policy layer | Низкий |
| Утечка локальной БД или `.env` | Низкая / критическое | filesystem audit, repo review, host controls | `.gitignore`, local-only storage, secrets only via env, no secret commit | Средний |
| Неправильный live send в реальный канал | Низкая / высокое | audit trail, operator observation | `dry_run` default, approval required, emergency stop | Низкий |

## 2. Политика логов

В текущем PoC применяются следующие правила:

- source-of-truth находится в локальной БД, а не в логах;
- audit events сохраняются отдельно от operational metrics;
- полный CV не должен дублироваться в application logs;
- raw Telegram bodies и conversation text допускаются в локальном storage для runtime и replay/eval;
- секреты не должны попадать в logs или в git.

Ограничение текущего PoC:

- полноценный JSON logging pipeline с формальной redaction policy не реализован;
- поэтому эксплуатационная рекомендация для PoC: считать локальную машину доверенной средой и не публиковать raw runtime artifacts.

## 3. Политика персональных данных

Текущий PoC работает с потенциально персональными данными:

- recruiter handle;
- recruiter email;
- тексты вакансий;
- тексты replies;
- профиль кандидата.

Правила:

- данные хранятся локально в `Postgres` volume и config files оператора;
- передача во внешнюю LLM-систему возможна только через `Astrixa`;
- внешние LLM вызовы ограничены задачами parsing/generation/classification/summarization;
- секреты и session strings должны храниться только в `.env` или локальном secret store;
- `.env` и `data/` не должны коммититься в репозиторий.

Что PoC не гарантирует:

- formal encryption-at-rest на уровне приложения;
- полное исключение передачи данных внешним LLM provider, если оператор включает реальный provider в `Astrixa`.

## 4. Подтверждение действий и stop controls

Обязательные control points:

- human approval перед first send;
- отдельный `queue-send` шаг перед dispatch;
- approval TTL;
- `emergency_stop`, блокирующий side effects;
- `dry_run` как режим по умолчанию;
- no duplicate dispatch to same `contact_target`;
- no follow-up after rejection.

## 5. Защита от injection

Модель доверия:

- Telegram post и recruiter reply рассматриваются как недоверенный пользовательский ввод;
- недоверенный ввод может использоваться только как data payload для parsing, summarization и generation;
- недоверенный ввод не интерпретируется как операторская команда или системная инструкция.

Практические меры:

- прямого tool execution из текста нет;
- policy enforcement выполняется детерминированным кодом, а не LLM;
- critical state transitions выполняются только через API и DB-backed state machine;
- side effects не исполняются по одному лишь ответу LLM.

## 6. Режимы отправки

Режимы transport layer:

- `dry_run`
  side effects не выполняются, но pipeline, audit и state transitions работают;
- `manual_send`
  выполняется реальный Telegram или email dispatch при наличии подтверждения и конфигурации.

Для демонстрации и сдачи PoC рекомендуемый режим: `dry_run`.
