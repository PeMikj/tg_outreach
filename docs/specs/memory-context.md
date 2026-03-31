# Memory and Context Spec

## Purpose

Определяет, какие данные система хранит как execution state, long-term memory и prompt context,
и как контролируется budget.

## State Layers

- `execution state`: job status, retry count, lock owner, timestamps
- `conversation state`: recruiter status, last outbound, last inbound, rejection flag, follow-up flag
- `long-term memory`: profile, CV, approved snippets, summaries
- `audit state`: operator actions, approvals, emergency stop changes

## Memory Policy

- Raw Telegram events immutable
- Summaries replace long message histories for prompt assembly
- Approved outreach snippets may be reused as templates
- Rejected or unsafe content is never promoted into reusable template memory

## Context Assembly Contract

Input:

- task type
- structured vacancy or reply
- conversation id or vacancy id
- token budget

Output:

- normalized context bundle
- included sources list
- estimated token usage
- truncation decisions

## Budget Rules

- Default context budget: 3k input tokens for PoC
- Prefer structured fields over raw text
- Prefer summaries over full chat history
- Hard fail to manual review if safe context cannot fit within budget

## Data Retention

- Raw events retained for audit/debug
- Conversation summaries retained as working memory
- Logs must not contain full CV or full chat bodies
