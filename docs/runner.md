# Persistent LLM runner

`llm run` classifies the conversations of a workspace with the configured provider. It
checkpoints every attempt, resumes where it stopped, reuses earlier results, isolates
individual failures, respects rate limits and budgets, and can be exported while it runs.

The provider is the offline `FakeProvider` by default. Since milestone 6 it can also be a real
provider ([providers.md](providers.md)); the runner itself is unchanged. With a real
provider, every invocation that may send content needs explicit authorization
(`--authorize-send` or an interactive confirmation), and the project collects no telemetry.

## Commands

```bash
whatsapp-analyzer llm run -w ./analysis                       # plan and execute (or resume)
whatsapp-analyzer llm run -w ./analysis --conversation alpha --conversation beta
whatsapp-analyzer llm run -w ./analysis --max-tokens 200000 --concurrency 4 --rpm 60
whatsapp-analyzer llm run -w ./analysis --retry-failed        # failed jobs only
whatsapp-analyzer llm run -w ./analysis --resend-unknown      # jobs awaiting reconciliation
whatsapp-analyzer llm run -w ./analysis --run 3               # resume or requeue run 3
whatsapp-analyzer llm status -w ./analysis [--run 3]
whatsapp-analyzer llm export -w ./analysis --out llm.csv [--run 3]
whatsapp-analyzer --json llm run -w ./analysis --events -     # NDJSON events on stderr
whatsapp-analyzer llm run -w ./analysis --force --fake-scenario error:TIMEOUT   # demos
```

| Exit code | Meaning |
|---:|---|
| 0 | Completed, or already up to date |
| 5 | Paused by a provider account error (authentication, permission, credits, model) |
| 6 | Interrupted (Ctrl+C or a crash); run the command again to resume |
| 7 | Budget reached: the next attempt's estimated worst case would not fit; resume with a higher limit |
| 8 | Finished with failed jobs or jobs awaiting reconciliation |

`--events FILE` (or `-` for stderr) writes one JSON object per line:

- events: `run_started`, `attempt_dispatched`, `attempt_finished`, `attempt_abandoned`,
  `retry_scheduled`, `job_reused`, `job_failed`, `budget_reached`, `run_paused`,
  `run_finished`;
- fields: identifiers, statuses and codes only.

With `--json`, stdout still carries exactly one JSON document. A progress bar goes to stderr
on a terminal.

## Runs, jobs and identity

**Run key.** A hash of the provider, the model, the template (id, version, hash), the context
settings with the masking version and the ignored texts, the output schema and the generation
parameters (`max_output_tokens`, `temperature`).

**Frozen scope.** When a run is planned, it gets one job per selected conversation, pointing at
that conversation's immutable version. Later imports create new versions and never change an
existing run: running again with the new data has a different scope fingerprint, so it plans a
new run.

**Request id.** Each job's request id hashes:

- the rendered prompt;
- the context hash;
- the model;
- the output schema;
- `max_output_tokens` and `temperature`.

Resuming rebuilds the request deterministically. If it no longer matches the plan, the job
fails with `CONTEXT_CHANGED` instead of sending something else.

**Same command again:**

| Situation | Result |
|---|---|
| A run with the same key and scope is incomplete | That run resumes |
| It completed | Up to date (exit 0) |
| It finished with open jobs | Unchanged (exit 8) until you pass `--retry-failed` or `--resend-unknown` |

**`--force`** plans a new run even when one exists.

**Reuse.** Before sending, a job whose provider and request id already have a successful
result reuses it. The job is marked `reused`, and no attempt or cost is recorded for it, so
historical spend is never copied. The provider identity includes the fake provider's demo
scenario (`fake:review`, `fake:error:TIMEOUT`), so demo results never feed a normal run.

**Identical requests.** Within a run, jobs with the same request id are one logical request,
wherever they sit in the queue. Before a job is sent, the runner checks the in-flight requests,
the scheduled retries and, in SQLite, the other jobs of the run with the same request id:

- in flight, or waiting for a scheduled retry (and its `retry_after`): the job waits for that
  attempt;
- a twin succeeded: the job reuses that result, even with `--no-reuse`, which only concerns
  results of other runs;
- a twin failed, including exhausted retries: the job fails too (`DUPLICATE_OF_FAILED`);
- a twin awaits reconciliation: the job does too (`DUPLICATE_OF_UNKNOWN`).

Because the check reads the run's own jobs from SQLite, it also holds after an interruption.
`--retry-failed` and `--resend-unknown` requeue all twins together, so the request is sent
once and the others share the new outcome. A job explicitly requeued by `--resend-unknown` is
never blocked by the state of a twin. If the owner of a scheduled retry ends without sending
(for example `CONTEXT_CHANGED`), its waiting twins take its outcome instead of waiting.

A request whose earlier attempt, in any run, has billing `unknown` is not sent again without
`--resend-unknown`, even with `--force --no-reuse`. `--resend-unknown` authorizes only the
jobs of the selected run that were awaiting reconciliation, not other jobs that happen to
share a request id.

**`--no-reuse`** calls the provider even when such a result exists, inside the run being
executed. It does not create a new run by itself: use `--force --no-reuse` to request
everything again.

## States

**Run:**

```
running -> completed | completed_with_failures | budget_reached | paused | interrupted
```

`budget_reached`, `paused` and `interrupted` resume when the command is run again.

**Job:**

```
pending -> in_flight -> succeeded | failed | retry_wait (-> pending) | awaiting_reconciliation
pending -> reused | skipped (group, no messages, context too large: before any call)
```

**Attempt:**

```
dispatched -> completed | abandoned
```

Each attempt also records its billing:

| Billing | Meaning | Automatic resend |
|---|---|---|
| `not_sent` | The request never left the machine (the adapter says so) | Yes, if the error is retryable |
| `rejected` | The provider refused it before processing (`RATE_LIMITED`, `OVERLOADED`, account errors, `INVALID_REQUEST`, `CONTEXT_TOO_LONG`) | Only `RATE_LIMITED` and `OVERLOADED` |
| `billed` | A response came back (success, invalid output, refusal, truncation) | No; `--retry-failed` resends a failed one |
| `unknown` | It may have been processed: `TIMEOUT`, `NETWORK`, `SERVER_ERROR`, `MALFORMED_PROVIDER_RESPONSE`, `ADAPTER_ERROR`, `ENDPOINT_ERROR` (any HTTP error from a non-official endpoint), cancellation after sending, or a crash | **Never**; only `--resend-unknown` |

A job whose attempt ended `unknown` becomes `awaiting_reconciliation`:

- it is listed in `llm status` and marked `needs_reconciliation` in exports;
- it keeps its budget reservation;
- it is sent again only with `--resend-unknown`, which may be billed twice;
- `--retry-failed` never includes it.

Every attempt is kept for audit.

**Account errors** pause the whole run at once. Attempts already in flight finish, the job
that hit the error stays `pending`, and the run exits with code 5.

**Safe retries** use exponential backoff from 1 s (deterministic jitter up to 25%, capped at
60 s), never shorter than the provider's `retry_after`. A job gets at most `max_attempts`
attempts (default 3) per round; `--retry-failed` and `--resend-unknown` start a new round.

## Checkpoints and resumption

Per attempt, with one writer on the event-loop thread:

1. **Reserve.** Wait for a concurrency slot and the rate limiter, check the budget including
   every reservation, and reserve the attempt's worst case.
2. **Transaction 1.** Insert the attempt as `dispatched` with billing `pending`, and mark the
   job `in_flight`. This commits **before** the provider is called.
3. **Call.** Call the provider, with a timeout.
4. **Transaction 2.** Record the outcome, billing, usage and cost on the attempt and the job.

| Interruption | Result after restart |
|---|---|
| Before the checkpoint | The job is still `pending` and is sent normally |
| After the checkpoint, during the call, or after the call but before step 4 | The attempt becomes `abandoned` with billing `unknown`, and the job awaits reconciliation |
| After step 4 | The result is kept; nothing is sent again |

The cases after the checkpoint cannot be told apart, so they are handled the same,
conservative way.

If recording an outcome fails unexpectedly (a bug or a database error), the runner stops
dispatching at once. It lets the attempts already sent finish, marks the unrecorded attempt
`unknown`, and ends the run `interrupted` with reason `INTERNAL_ERROR`.

The workspace writer lock means only one process runs jobs in a workspace at a time. A run
found `running` by the next writer therefore crashed: it becomes `interrupted` with reason
`CRASH`, and its dispatched attempts are recovered as above. Ctrl+C cancels the attempts in
flight (they become `unknown`), marks the run `interrupted`, and exits with code 6. `llm status`
never writes: it shows such a crashed run as `running` with a notice (`stale` in JSON) until
the next writer recovers it. To tell, it takes the writer lock for an instant; a writer
started at exactly that moment fails with `WORKSPACE_BUSY` and can simply be run again.

**Delivery guarantee.** Exactly-once delivery cannot be promised without idempotency support
from the provider. The runner sends each checkpointed attempt automatically at most once,
never loses a confirmed result, and never requests a confirmed request id again unless
`--no-reuse` is given.

## Budgets, reservations and limits

```toml
[llm.limits]
concurrency = 4
requests_per_minute = 60
# tokens_per_minute = 100000
max_attempts = 3
# max_tokens = 200000
# max_cost = "1.00"
```

Every option can also be given on the command line: `--concurrency`, `--rpm`, `--tpm`,
`--max-tokens`, `--max-cost` and `--max-attempts`.

- **Scope:** limits apply to the whole run, across resumptions.
- **New run:** the limits come from `analyzer.toml` plus the options given.
- **Resumed run:** it keeps the limits stored when it started, and changes only the options
  given explicitly on the command line. Leaving out `--max-tokens` therefore never removes a
  budget, and editing `analyzer.toml` does not change an existing run, whether the edit
  raises or lowers a limit. Pass the option explicitly to change it.
- **Per run, not per workspace:** a run planned with `--force` or over a new scope starts
  with its own budget, and there is no workspace-wide cap.

**Reservation.** The estimated input tokens plus the maximum output tokens. With a price, the
reservation is that many tokens at the highest of the input, cache and output rates.

**Charge.** When an attempt finishes:

- a billed attempt with complete usage charges the reported counts (or the local estimates of
  the fake provider) and its cost;
- a billed attempt with any count or cost unknown, or an attempt with billing `unknown`, keeps
  its reservation;
- `not_sent` and `rejected` attempts charge nothing.

Unknown values are never treated as zero.

**Dispatch check.** An attempt is dispatched only if consumption, plus in-flight
reservations, plus unknown-billing reservations, plus its own worst case fit every limit.
Decisions and reservations happen on one thread before any await, so concurrent attempts
cannot overshoot together. When the next attempt does not fit, the run stops as
`budget_reached`.

**`--max-cost`** needs a complete price entry (input, output, cache read and cache write).
Otherwise the command refuses to start (`BUDGET_UNVERIFIABLE`), because the limit could not
be guaranteed.

**Rate limits.** `requests_per_minute` and `tokens_per_minute` are token buckets that start
full and refill continuously. The token bucket uses the same reservations.

**Estimates.** Token reservations rely on local estimates. They are conservative for the fake
provider and for typical text, but they are not mathematical guarantees for real providers,
whose tokenizers differ.

`llm status` (table and JSON) and the export metadata report these separately. A cost is
shown as unknown, never as zero, when any part of it has no price; a known total that leaves
out billed attempts without a cost says so:

- reported tokens;
- locally estimated tokens;
- known cost;
- billed attempts without a cost;
- in-flight reservations;
- unknown-billing reservations;
- committed budget.

## Storage and privacy

Migration 2 adds `llm_runs`, `llm_jobs` and `llm_attempts`.

- **Stored:** hashes, version references, statuses, billing, error and validation codes, label
  and reason codes, evidence message ids, security flags, token counts, costs and
  reservations.
- **Not stored:** message text, rendered prompts and raw provider responses. Message text
  already lives in the workspace's own tables (milestone 3).

Events and exports follow the same rule, and a test scans every new table, event and export
for synthetic content.

**Export.** `llm export` writes a CSV and `FILE.meta.json` from one WAL snapshot:

- `run_partial` and the `partial` metadata flag mark runs that may still change;
- `data_version` marks jobs computed from a superseded conversation version;
- `needs_reconciliation` marks jobs awaiting reconciliation.

Conversation keys are escaped against spreadsheet formulas.

## Budget of resumed runs

When a run starts or resumes, the ledger is rebuilt from its attempts. Each billed attempt
counts at least the cost of its stored usage at the run's price snapshot, and each in-flight
or unknown reservation at least its stored reserved tokens at the snapshot's worst-case rate.
For a run whose snapshot never changed this is exactly what was recorded. When a snapshot
gained a price tier it lacked (see [providers.md](providers.md#tokens-and-costs)), attempts
recorded at the cheaper rate count at the applicable one before anything else is dispatched.
The recorded costs are never rewritten; `budget.cost_adjustment` reports the difference.

## Limitations

- **Billing classes of real providers** depend on each adapter reporting `request_sent`
  accurately; ambiguous transport failures stay `unknown` (see
  [providers.md](providers.md#errors)).
- **Ctrl+C.** In-flight attempts become `unknown` even when the fake provider would have
  answered.
- **Token limits.** Based on estimates (see above).
- **Retry waits.** Waiting jobs are not persisted with their wake-up time. A resumed run sends
  them without waiting the rest of the backoff.
