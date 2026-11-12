# ADR 0007: Real LLM providers, BYOK and authorization

- Status: Accepted
- Date: 2026-11-10

## Context

Milestone 6 connects the M5 runner to real providers. The tool is free and local-first: each
person brings their own key and pays the provider directly, there is no backend, and the
content is private conversations. The runner, the database and the classification must not
change, other developers must be able to add providers, new models must work without code
changes, and nothing may leave the machine without an explicit decision.

## Decision

- **Official SDKs as optional extras** (`[openai]`, `[anthropic]`, `[providers]`), imported
  only when a provider is built. Clients are created with `max_retries=0` (the runner owns
  every attempt), an explicit key and base URL (SDK environment defaults never apply), an
  explicit timeout, tracing disabled for Anthropic, OpenAI organization/project headers
  removed, and an HTTP client built by this project: redirects are never followed (a 3xx is a
  refused request), and proxies are ignored for loopback endpoints. SDK variables that inject
  headers (`*_CUSTOM_HEADERS`) are refused while set.
- **Two adapters, three providers.** `openai_chat.py` (Chat Completions) serves `openai` and
  `openai-compatible`; `anthropic_messages.py` serves `anthropic`. The Responses API is left
  for later and fits next to them without structural changes.
- **A plain registry** (`llm/providers/__init__.py`): one `ProviderSpec` per provider. The
  configuration reads valid provider names from it, so registering is the only step outside
  the adapter. No plugin framework.
- **`build_provider` checks everything before planning**: options against the provider's
  allowlist, temperature and output limits against the model, the endpoint, the SDK, SDK
  debug logging and the API key. Failures are stable codes with exit code 4.
- **An open model catalog** (`models.toml`, built-in plus workspace). Capabilities that are
  not declared are unknown and never guessed: an undeclared structured-output mode falls back
  to prompt-only JSON, an undeclared temperature is refused, unknown limits are not checked.
  Known context limits skip jobs before sending (`MODEL_CONTEXT_EXCEEDED`, with a 1.25 margin
  on local estimates).
- **BYOK through environment variable names.** The configuration holds `api_key_env` only.
  Keys live in `SecretValue`, are registered for redaction, cannot be pickled and never reach
  files, the database, events, exports or errors.
- **Endpoint identity** = provider plus scheme, host, port and base path. HTTPS is required
  except for loopback. The identity is the provider name stored with attempts, so reuse and
  authorization never cross endpoints.
- **Per-invocation authorization** (`cli/consent.py`). Every `llm run` that may send needs
  either an interactive confirmation (typing the destination identity after seeing the
  estimate and the privacy notice) or `--authorize-send IDENTITY` matching the configured
  destination. Without it: exit code 12, nothing sent, the run planned, and machine-readable
  details (estimate, notice, exact flag) for agents. Never implied by environment variables or
  configuration; never lifts budgets or limits. Migration 3 adds `llm_authorizations`
  (time, method, destination, model, notice version), additive only.
- **Error mapping shared by adapters** (`_http.py`): HTTP status plus documented error codes
  to `ErrorKind`; `request_sent=False` only when the transport proves the connection was never
  established; any HTTP response means sent; everything ambiguous stays unknown so the M5
  judge sends it to reconciliation. Only the official APIs, at their default URL, get
  "rejected" kinds (their documented refusals; 529 only for Anthropic). Every HTTP error from
  any other endpoint is `ENDPOINT_ERROR` (billing unknown, reconciliation): a gateway can
  answer an error after the model ran. Rate limits and lack of credits are distinguished on
  the official APIs (an OpenAI 429 `insufficient_quota` pauses the run instead of retrying).
- **Usage and prices.** Adapters fill disjoint buckets with `null` for anything not reported;
  reasoning stays inside `output_total`. A missing cache-write count is zero only when the
  model is declared as never billing cache writes. Real prices ship with source and retrieval
  date and are flagged stale after 90 days. Tiered prices declare a ceiling
  (`max_input_tokens`) and long-context rates; the tier is chosen from the reported input
  total (unknown: the long rates), reservations use the highest rate of every tier, and a
  resumed run's snapshot gains tier information it lacked. A ceiling without long rates skips
  larger prompts (`PRICE_TIER_EXCEEDED`) and cannot back a hard cost limit. Request ids include provider parameters (output
  mode, options) only when present, so earlier fake-provider request ids are unchanged.
- **Agents use the CLI**, documented in `docs/agent-usage.md` and `AGENTS.md`. No MCP server.

This satisfies, more strictly, the authorization requirement recorded in ADR 0005 and
`docs/llm.md`: instead of a stored authorization per workspace tied to a hash, every
invocation that may send is authorized anew, and the run it belongs to is tied to the run key
(provider, model, prompt and generation parameters).

## Consequences

- Real runs need the person's explicit action each time, including resumes; agents must ask.
- Model capabilities and prices need maintenance; stale prices are flagged, unknown ones are
  never invented, and workspace files override them without code changes.
- Correctness of billing classes depends on each adapter's `request_sent`; the conservative
  default is unknown, which can leave more jobs awaiting reconciliation.
- OpenAI-compatible endpoints vary; their capabilities and prices are the user's declaration.
- No paid call runs in tests or CI: adapters are tested with mock transports, and the single
  live smoke test is opt-in, authorized explicitly and capped in cost.
