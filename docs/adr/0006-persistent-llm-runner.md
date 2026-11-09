# ADR 0006: Persistent LLM runner

- Status: Accepted
- Date: 2026-11-09

## Context

Classifying thousands of conversations must survive interruptions and crashes. It must never
silently send a request twice when the first may have been billed, keep results tied to the
exact data they came from, stay within rate limits and budgets, and remain inspectable and
exportable while it runs. Milestone 5 must work entirely offline with the `FakeProvider`.

## Decision

- **Three new tables** (migration 2): `llm_runs`, `llm_jobs` and `llm_attempts`. They hold
  hashes, references, statuses, codes, counts and costs, never message text, prompts or raw
  responses.
- **Frozen scope.** A run's jobs reference immutable conversation versions and are created in
  one transaction at planning. The run key covers provider, model, template, context settings
  and generation parameters. The request id additionally covers the rendered prompt, the
  context and the output schema.
- **Write-ahead attempts.** The attempt row is committed before the provider call and
  completed in a second transaction. Anything dispatched without a recorded outcome is
  `unknown`.
- **Explicit billing classification** (`not_sent`, `rejected`, `billed`, `unknown`) and a job
  state `awaiting_reconciliation`. Unknown outcomes are never resent automatically, only with
  `--resend-unknown`, and they keep their budget reservation.
- **Reservation-based budgets.** The worst case uses estimated tokens and the highest rate.
  The dispatch decision and the reservation happen on the single event-loop thread before any
  await, so concurrency cannot overshoot. A hard cost limit is refused without a complete
  price.
- **Reuse by `(provider, request_id)`**, without copying historical cost. `--force` and
  `--no-reuse` are separate, documented controls.
- **Run-wide coordination of identical requests.** Jobs of one run that share a request id
  never send it twice without authorization: a twin waits while the request is in flight or
  has a scheduled retry, then shares its stored outcome (reused result, `DUPLICATE_OF_FAILED`
  or `DUPLICATE_OF_UNKNOWN`). The check reads the run's jobs from SQLite, so it survives
  interruptions; only an explicit `--resend-unknown` or `--retry-failed` sends again.
- **asyncio runner** with a semaphore and token buckets on an injectable clock, as chosen in
  ADR 0005. The workspace writer lock serializes runners across processes.
- **NDJSON events** for agents, separate from the single JSON result on stdout.

## Consequences

- Correctness does not depend on exactly-once delivery, which providers cannot guarantee
  without idempotency keys. The runner offers at-most-once automatic sending per checkpoint
  and never loses confirmed results.
- A crash or Ctrl+C can leave jobs awaiting reconciliation that a human (or an agent with
  explicit authorization) must resolve.
- Token budgets are only as good as local estimates for real providers. This is documented as
  a guide, not a guarantee.
- Milestone 6 adapters must classify their errors and report `request_sent` faithfully, or
  the conservative `unknown` path applies.
