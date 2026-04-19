# Retriever Spec

## Purpose

Обеспечивает сбор релевантного контекста для scoring, draft generation и reply classification
без отдельного retrieval service и без обязательного embedding-контура.

## Sources

- User CV and profile
- User preferences and constraints
- Historical approved outreach
- Historical conversation summaries
- Recruiter profile snippets

## Storage and Index

- Structured metadata хранится в `Postgres`
- Retrieval выполняется in-process внутри `outreach-api` / `outreach-worker`
- Отдельный lexical engine и vector index не требуются для текущего PoC

## Query Contract

Input:

- `query_type`: `score_context | draft_context | reply_context`
- `vacancy_id` or `conversation_id`
- `top_k`
- optional filters: `role_family`, `language`, `source_type`

Output:

- ordered list of context chunks
- source labels
- retrieval mode: `in_process_context_bundle`
- truncation metadata

## Search Pipeline

1. Metadata filtering
2. Deterministic selection of profile, preferences, recruiter memory, summaries and approved snippets
3. Budget-aware chunk selection
4. Truncation decisions stored in `context_bundle`

## Constraints

- `top_k` behavior задается через in-process builder
- Retrieval не должен обращаться к внешним сервисам кроме уже сохраненного local state
- Недостаток контекста не должен валить workflow целиком

## Failure Handling

- On low-context result -> use template fallback and raise low-context flag
- On oversized context -> drop low-priority chunks and keep structured fields first

## Telemetry

- context assembly latency
- low-context rate
- chunk count and token estimate
- truncation frequency
