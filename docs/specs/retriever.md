# Retriever Spec

## Purpose

Обеспечивает сбор релевантного контекста для scoring, draft generation и reply classification,
при этом сохраняет работоспособность системы даже при недоступности embedding/rerank-контура.

## Sources

- User CV and profile
- User preferences and constraints
- Historical approved outreach
- Historical conversation summaries
- Vacancy history

## Storage and Index

- Structured metadata хранится в Postgres
- Lexical index обязателен
- Vector index опционален для PoC, но интерфейс под него резервируется

## Query Contract

Input:

- `query_type`: `score_context | draft_context | reply_context`
- `vacancy_id` or `conversation_id`
- `top_k`
- optional filters: `role_family`, `language`, `source_type`

Output:

- ordered list of context chunks
- per-chunk score
- retrieval mode: `lexical | hybrid`
- truncation metadata

## Search Pipeline

1. Metadata filtering
2. Lexical candidate retrieval
3. Optional semantic rerank
4. Score thresholding
5. Budget-aware chunk selection

## Constraints

- `top_k` default 5, max 10
- Retrieval timeout: 800 ms soft, 1500 ms hard
- Missing embeddings must not fail the whole workflow

## Failure Handling

- On vector failure -> lexical-only mode
- On timeout -> return best partial lexical result
- On empty result -> use template fallback and raise low-context flag

## Telemetry

- retrieval latency
- hit rate
- empty-result rate
- hybrid vs lexical-only ratio
- chunk count and token estimate
