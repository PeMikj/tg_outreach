# Agent / Orchestrator Spec

## Purpose

Управляет multi-agent workflow как явной state machine и является единственной точкой,
которая координирует handoff между агентами и разрешает side effects после policy
и idempotency checks.

## Main Steps

1. route raw event to `Ingestion Agent`
2. hand off structured vacancy to `Matching & Decision Agent`
3. build context and call `Generation Agent`
4. run `Execution & Safety Agent` prechecks
5. wait for approval
6. revalidate TTL/policy before send
7. perform side effect
8. persist result and schedule next action

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
