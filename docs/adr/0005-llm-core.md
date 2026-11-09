# ADR 0005: Offline LLM core

- Status: Accepted
- Date: 2026-11-08

## Context

Conversations will be classified by language models from several providers. Before any real
adapter or paid call exists, the project needs the following:

- a provider-independent contract;
- versioned prompts;
- context preparation that protects identifiers;
- strict output validation;
- token and cost accounting that never double counts or guesses;
- an error taxonomy;
- a way to preview what would be sent, without sending anything.

Milestone 4 must stay fully offline.

## Decision

- **Three separate outcome axes.** These are the execution status (`SUCCESS`,
  `INVALID_RESPONSE`, `REFUSED`, `CONTENT_FILTERED`, `INCOMPLETE_OUTPUT`, `FAILED` with an
  `ErrorKind`), the classification label (`HIGH`, `MEDIUM`, `LOW`, `REVIEW_REQUIRED`, only on
  success) and security flags. `REVIEW_REQUIRED` is a valid conclusion of ambiguity, never a
  stand-in for a failure. Flags are risk signals and never change a label.
- **Asynchronous provider protocol.** `async def complete(request) -> LLMResult`, with timeouts
  and cancellation handled by the caller (`classify`).
- **Markdown templates with TOML front matter.** They have two sections and `{{name}}`
  placeholders with no logic, and they are hashed. The built-in ones are pinned in a manifest
  checked by tests.
- **Context builder.** It keeps the canonical order, labels senders only by role, masks
  identifiers (documented as pseudonymization, not anonymization), escapes delimiters, indents
  continuation lines, and truncates explicitly within a token budget. It fails rather than cut
  silently.
- **Closed output schema.** The output has a level, reason codes about observable behavior and
  evidence message ids, with no free text and no confidence. It is validated locally.
- **Disjoint usage buckets.** `null` means unknown, and reasoning is a subset of the output
  total. Decimal prices are versioned with source and effective date. No real prices ship in
  milestone 4, and an unknown cost stays `null`.
- **Preview.** It shows structure only by default. Content output needs a confirmation specific
  to content. The output file is written with mode 0600, never overwrites silently, refuses
  symlinks, and refuses Git work trees outside workspaces.
- **No new dependencies.** The core uses `typing.Protocol`, `asyncio`, `tomllib`, `decimal`,
  `re` and `html`. A hand-written validator replaces `jsonschema`, and a heuristic estimate
  replaces provider tokenizers.

### Threads or asyncio

| Concern | Threads (sync interface) | asyncio (async interface) |
|---|---|---|
| Real SDKs (milestone 6) | Both official SDKs have sync clients | Both have async clients; a sync-only SDK can run through `asyncio.to_thread` |
| Concurrency and rate limits (milestone 5) | Pool size plus locks; token-bucket state shared across threads | Semaphores and a single-threaded limiter, with no locks |
| Cancellation and pause | Cooperative only; an HTTP call in flight cannot be interrupted | `task.cancel()` interrupts at the next await; pause means stop dispatching |
| Timeouts | Only those the HTTP client enforces | `asyncio.timeout` / `wait_for` around any call |
| Testability | Real threads make timing tests flaky | Deterministic on one event loop; no plugin needed (`asyncio.run`) |
| Resources | One OS thread per request in flight | One thread; cheap tasks |
| Maintenance | Familiar, but shared state needs locks | async spreads up the call stack, kept to the runner and the adapters |

An async interface can host sync implementations, while a sync interface cannot gain clean
cancellation later. So the protocol is async. The CLI stays synchronous and calls
`asyncio.run` at its edge. SQLite writes in milestone 5 stay on one writer, the event-loop
thread, which matches the single-writer workspace lock.

## Consequences

- Milestone 5 can schedule `classify()` coroutines with bounded concurrency, rate limiting,
  pause and cancellation without changing the contract.
- Token estimates are rough by design, and the preview labels them as estimates. Exact counts
  come from provider usage.
- Masking is best-effort pseudonymization, not guaranteed anonymization: it reduces but does
  not remove personal data. Known names in some Unicode representations can remain visible
  (accepted residual risk), and normalization can alter emoji sequences and some scripts. The
  documentation and the CLI say so explicitly.
- Local mode sends nothing to external services. Milestone 6 must add an explicit,
  provider-specific authorization before any content is sent. That authorization must also
  work non-interactively, and it must never disable the other safeguards.
- Heuristic injection flags have false positives and false negatives. They are signals for
  review. The closed schema and the evidence checks limit what a successful injection could
  produce, but they cannot detect one.
