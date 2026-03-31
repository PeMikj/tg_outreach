# Agent / Orchestrator Spec

## Purpose

Управляет всеми workflow как явной state machine и является единственной точкой,
которая может инициировать side effects после прохождения policy и idempotency checks.

## Main Steps

1. ingest raw event
2. parse vacancy or reply
3. retrieve context
4. score / classify
5. policy decision
6. generate draft if allowed
7. wait for approval
8. revalidate before send
9. perform side effect
10. persist result and schedule next action

## Transition Rules

- Only one active execution per job key
- Side-effect steps require fresh policy check
- Approval TTL expiration forces re-approval
- Send is forbidden if conversation is closed, rejected, duplicated or above limit

## Stop Conditions

- permanent policy deny
- operator reject
- emergency stop
- unrecoverable external error
- max retry budget exhausted

## Retry Policy

- transient internal step errors: exponential backoff, max 3 attempts
- Telegram send transient error: max 2 attempts
- LLM transient error: max 1 retry, then fallback/manual review
- DB transaction conflict: short retry with jitter

## Recovery

- On restart, worker scans jobs in `running` or `retry_pending`
- stale locks are reclaimed after lease timeout
- idempotency guard prevents duplicate send after crash/restart

## Required Telemetry

- step duration
- queue age
- retry count
- failure reason
- state transition count
