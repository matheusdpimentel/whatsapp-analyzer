# LLM providers

The analyzer is free and BYOK (bring your own key): you choose a provider, use your own API
key and pay the provider directly, if it charges. There is no backend, account or
subscription of this project, and requests go straight from your machine to the provider you
configure. Without a real provider, everything works offline with the `fake` provider.

| Provider | `provider =` | Protocol | SDK extra |
|---|---|---|---|
| Offline fake (default) | `fake` | none, local and free | none |
| OpenAI | `openai` | Chat Completions | `openai` |
| Anthropic | `anthropic` | Messages API | `anthropic` |
| OpenAI-compatible endpoint | `openai-compatible` | Chat Completions at your `base_url` | `openai` |

Three different things are easy to confuse:

1. **A new model of a supported provider**: configuration only (`model = "..."`, plus an entry
   in `<workspace>/models.toml` and `<workspace>/pricing.toml` if you want its limits checked
   and its cost computed).
2. **A new OpenAI-compatible endpoint** (a hosted service or a local server): configuration
   only (`provider = "openai-compatible"`, `base_url`, and the capabilities you declare).
3. **A provider with a different protocol** (for example Gemini's native API): a new adapter,
   which is code. See [adding-a-provider.md](adding-a-provider.md).

## Install

```bash
pip install 'whatsapp-conversation-analyzer[anthropic]'   # or [openai], or [providers] for both
uv sync                                                    # development: both SDKs are dev dependencies
```

The base install needs no SDK. A missing SDK is reported as `PROVIDER_SDK_MISSING` with the
command to install it.

## Configure

`analyzer.toml` in the workspace (or a file given with `llm run --config FILE`):

```toml
[llm]
provider = "anthropic"
model = "claude-haiku-5-5"
prompt = "engagement-classification"
# max_output_tokens = 600      # default: the prompt template's value
# temperature = 0.0            # only for models that accept it, see below

[llm.connection]
api_key_env = "ANTHROPIC_API_KEY"   # the NAME of the variable, never the key
# base_url = "https://..."          # required for openai-compatible, optional otherwise
timeout_seconds = 120

[llm.options]
# effort = "low"                    # provider-specific, see "Options"

[llm.limits]
concurrency = 4
requests_per_minute = 50
tokens_per_minute = 40000
max_attempts = 3
max_tokens = 2000000
max_cost = "5.00"
```

Then export your key in the shell that runs the analyzer:

```bash
export ANTHROPIC_API_KEY="your-own-key"
# or any variable you like, with api_key_env = "MY_CUSTOM_LLM_KEY":
export MY_CUSTOM_LLM_KEY="your-own-key"
```

Rules for credentials:

- The key is never written to `analyzer.toml`, the database, logs, events, exports or JSON
  output, and is never shown in errors. Only the variable name is configured.
- Without `api_key_env`, each provider reads its documented variable and nothing else:
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_COMPAT_API_KEY`. The key and the URL are
  always passed to the SDK explicitly, so the SDKs' own defaults (`*_BASE_URL`,
  `ANTHROPIC_AUTH_TOKEN`, profiles) never apply. `OPENAI_ORG_ID` and `OPENAI_PROJECT_ID` are
  never sent. `OPENAI_CUSTOM_HEADERS` and `ANTHROPIC_CUSTOM_HEADERS`, which would add headers
  (even credentials) to every request, are refused while set (`SDK_ENV_OVERRIDE`).
- A missing, empty or malformed variable stops the run before anything is planned
  (`API_KEY_MISSING`, `API_KEY_EMPTY`, `API_KEY_MALFORMED`, exit code 4).
- `OPENAI_LOG=debug` or `ANTHROPIC_LOG=debug` is refused (`SDK_DEBUG_LOGGING`): SDK debug logs
  can contain request content.

### Endpoints

`base_url` must use HTTPS. Plain HTTP is accepted only for loopback (`localhost`,
`127.0.0.1`, `::1`). User names, passwords, query strings, fragments, backslashes and
non-ASCII host names are rejected (write internationalized names in their `xn--` form;
underscores, as in Docker service names, are accepted), so
the identity you authorize is exactly the host that is contacted. Redirects are never
followed: a 3xx answer is a refused request (`INVALID_REQUEST`, code
`REDIRECT_NOT_FOLLOWED`), never a second request elsewhere. For a loopback endpoint, proxy
settings (environment and system) are ignored, so the traffic stays on the machine. The
destination identity, used for authorization, reuse and the attempt records, includes the
scheme, host, port and base path, for example
`openai-compatible@https://api.example.com:443/v1`. Results are never reused across
different endpoints.

## Models: an open catalog

Any model id can be configured. The built-in catalog (`src/whatsapp_analyzer/llm/models.toml`)
lists verified models with their source and retrieval date; `<workspace>/models.toml` adds
models or replaces built-in entries, with the same format:

```toml
schema = 1

[[models]]
provider = "openai-compatible"
model = "my-local-model"
context_window = 32768          # input + output
# max_input_tokens = 30000      # when lower than the context window
max_output_tokens = 4096
temperature = true              # the endpoint accepts a temperature
structured_output = "json_object"   # json_schema | json_object | none
cache_write_billed = false      # this endpoint never bills cache writes
prompt_caching = false          # no prompt cache: unreported cache counts are zero
source = "https://docs.example.com/models"
retrieved_at = 2026-11-10
```

Nothing is guessed for a model that is not listed:

| Unknown capability | Behavior |
|---|---|
| `structured_output` | JSON is requested by the prompt only (`none`) and validated locally. Notice `STRUCTURED_OUTPUT_UNDECLARED`. |
| `temperature` | A configured temperature is refused (`TEMPERATURE_UNDECLARED`) until you declare `temperature = true`. Never dropped silently. |
| `context_window`, `max_input_tokens` | Prompts are not checked against them. Notice `CONTEXT_WINDOW_UNKNOWN`. |
| `max_output_tokens` | Not checked. |
| `cache_write_billed` | A cache-write count the provider does not report stays unknown. |
| `prompt_caching` | Unreported cache counts stay unknown, so a server that reports only `prompt_tokens` has an unknown input split (and cost) until you declare `prompt_caching = false`. |

Known limits are enforced before sending: `max_output_tokens` above the model's limit is a
configuration error (`MAX_OUTPUT_EXCEEDS_MODEL`), and a job whose estimated prompt (times a
1.25 safety margin, because local estimates differ from each provider's tokenizer) plus
`max_output_tokens` cannot fit is skipped with `MODEL_CONTEXT_EXCEEDED`, without a request.

Anthropic's API version (2026-10-21, the SDK's) rejects sampling parameters on every model,
so `temperature` is always refused for `anthropic`.

### Structured output modes

| Mode | OpenAI / compatible | Anthropic |
|---|---|---|
| `json_schema` | `response_format: json_schema` with `strict: true` | `output_config.format` |
| `json_object` | `response_format: json_object` | not available (use `json_schema` or `none`) |
| `none` | prompt only | prompt only |

The schema sent to a provider omits `uniqueItems`, `maxItems`, `pattern` and `minItems` above
1, which structured-output modes commonly reject. Local validation still enforces the full
schema in every mode, so the label is always checked by the analyzer itself.

### Options

`[llm.options]` accepts only documented names; anything else is `OPTION_UNSUPPORTED`. Options
are part of the request id and the run key.

| Provider | Option | Values |
|---|---|---|
| `anthropic` | `effort` | `low`, `medium`, `high`, `xhigh`, `max` (`output_config.effort`) |
| `openai`, `openai-compatible` | `reasoning_effort` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` (support depends on the model) |
| `openai-compatible` | `token_limit_parameter` | `max_completion_tokens` (default) or `max_tokens`, for servers that only accept the older name |

## Authorization before sending

Every `llm run` that may send content to a real provider needs explicit authorization, for
that invocation only: a new run, a resumed run, `--retry-failed` and `--resend-unknown`
alike. See [agent-usage.md](agent-usage.md#authorization) for the agent flow.

- **Interactive** (a terminal, no `--json`): the destination, model, number of requests,
  worst-case tokens and cost and the privacy notice are shown; type the destination identity
  to confirm.
- **Non-interactive**: `--authorize-send IDENTITY`, exactly the configured identity
  (`anthropic`, `openai`, `openai-compatible@https://host:443/path`). Any other value is
  refused before anything is planned (`AUTHORIZATION_MISMATCH`).
- Without it: exit code 12, `AUTHORIZATION_REQUIRED`, with `details.estimate` (what would be
  sent), `details.notices` and `details.authorize_with` (the exact flag). Nothing was sent:
  the run (`details.run_id`) is planned locally, left `interrupted` with
  `stop_reason: AUTHORIZATION_REQUIRED`, and resumes when authorized.
- It is never implied by an environment variable or a configuration file, and it never lifts
  budgets, rate limits or reconciliation rules.
- The privacy notice states that the prepared text (masked messages, the prompt and the
  instructions) is sent, that masking is best-effort and not anonymization, and, for a
  loopback endpoint, that the local service may still forward data elsewhere.
- Each authorization is recorded in the `llm_authorizations` table: time, method, destination,
  model and notice version. Nothing else.

Commands other than `llm run` (`import`, `metrics`, `llm preview`, `llm status`,
`llm export`, ...) never build a real provider and never contact the network. Token counts
are always local estimates: no provider tokenization endpoint is called.

## Tokens and costs

Every adapter maps the provider's usage into disjoint buckets. `null` means "not reported"
and is never turned into zero.

| Bucket | OpenAI / compatible | Anthropic |
|---|---|---|
| `input_uncached` | `prompt_tokens - cached_tokens - cache_write_tokens` (only when both are known) | `input_tokens` |
| `input_cache_read` | `prompt_tokens_details.cached_tokens` | `cache_read_input_tokens` |
| `input_cache_write` | `prompt_tokens_details.cache_write_tokens`; `0` only when `cache_write_billed = false` | `cache_creation_input_tokens` |
| `output_total` | `completion_tokens` (reasoning included) | `output_tokens` (thinking included) |
| `output_reasoning` | `completion_tokens_details.reasoning_tokens` | `output_tokens_details.thinking_tokens` |

Reasoning tokens are part of `output_total` and are never counted twice. A reasoning count
larger than the output total is dropped as unknown.

Costs are **estimates** from the configured prices, never an exact bill:

- Prices live in the built-in `prices.toml` and in `<workspace>/pricing.toml` (which wins);
  each entry has its source, `effective_from` and `retrieved_at`. An entry retrieved more than
  90 days ago is reported as `PRICE_STALE`; a model without a price as `PRICE_MISSING`.
- `--max-cost` needs a complete price (input, output, cache read and cache write); otherwise
  the run is refused (`BUDGET_UNVERIFIABLE`), never run without protection.
- **Tiered prices.** An entry may cover only requests with at most `max_input_tokens` input
  tokens (cached and written tokens included); above it, the `long_input`, `long_cache_read`,
  `long_cache_write` and `long_output` rates apply to the whole request:

  ```toml
  max_input_tokens = 272000
  long_input = "0.20"
  long_cache_read = "0.02"
  long_cache_write = "0.25"
  long_output = "0.75"
  ```

  The tier is chosen from the input total the provider reports (`<=` the ceiling: short;
  above: long); when that total is unknown, the long rates are used, never the cheaper ones.
  Reservations use the highest rate of every tier, so a run can never dispatch past
  `--max-cost` because of the tier. An entry with a ceiling but no long rates cannot price
  larger requests: jobs that may exceed it are skipped (`PRICE_TIER_EXCEEDED`), their cost is
  unknown if one does, and such an entry cannot back `--max-cost` (`BUDGET_UNVERIFIABLE`).
  A run keeps the price snapshot it started with; a snapshot without tier information gains
  the ceiling and long rates of the current entry for the same model when the run resumes.
  The attempts it recorded before are then charged to the budget at the snapshot's rate for
  their stored usage (or their stored reserved tokens when the usage is unknown), never below
  what was recorded. The recorded costs stay unchanged in `llm export` and `cost.known_total`;
  `budget.cost_adjustment` in `llm status` shows how much was added. Nothing is re-sent to
  learn a cost, and the correction is derived on every load, so it never accumulates.
- Prices are looked up by provider name and model, not by endpoint: two
  `openai-compatible` endpoints serving the same model id share a `pricing.toml` entry, and
  `anthropic` with a custom `base_url` uses the official price.
- A response without usage is billed at its reservation (estimated input plus maximum output),
  its cost is unknown and the run's cost totals become unknown. It is never treated as free.
- Every attempt reserves its worst case before it is sent; reservations of attempts in flight
  and of unknown outcomes stay charged (see [runner.md](runner.md)).

Built-in prices (retrieved 2026-11-10 and 2026-11-11, USD per million tokens):

| Model | Input | Cache read | Cache write | Output | Source |
|---|---:|---:|---:|---:|---|
| `anthropic` `claude-haiku-5-5`, prompts up to 100,000 tokens | 0.10 | 0.01 | 0.125 | 0.50 | [pricing](https://platform.claude.com/docs/en/about-claude/pricing) |
| `anthropic` `claude-haiku-5-5`, prompts over 100,000 tokens | 0.50 | 0.05 | 0.625 | 2.50 | [pricing](https://platform.claude.com/docs/en/about-claude/pricing) |
| `anthropic` `claude-sonnet-5-5` | 2 | 0.10 | 2.50 | 10 | [pricing](https://platform.claude.com/docs/en/about-claude/pricing) |
| `anthropic` `claude-opus-5-5` | 4 | 0.20 | 5 | 20 | [pricing](https://platform.claude.com/docs/en/about-claude/pricing) |
| `openai` `gpt-6-luna`, up to 272K input tokens | 0.10 | 0.01 | 0.125 | 0.50 | [pricing](https://developers.openai.com/api/docs/pricing) (Standard tier, 30-minute cache write) |
| `openai` `gpt-6-luna`, over 272K input tokens | 0.20 | 0.02 | 0.25 | 0.75 | [pricing](https://developers.openai.com/api/docs/pricing) |

Check the provider's page before relying on them, and override them in `pricing.toml`.

## Errors

The adapters never retry (`max_retries=0`): every attempt is the runner's, so it is
checkpointed, budgeted and visible in `llm status`. Errors are mapped to the kinds of
[llm.md](llm.md#errors); the runner then decides billing and the next step.

**Official APIs** (`openai`, `anthropic` at their default URL) document which errors are
refused before processing, so their statuses map as follows:

| What happened | Kind | `request_sent` | Runner |
|---|---|---|---|
| Connection refused, DNS failure, connect timeout, no pooled connection | `NETWORK` / `TIMEOUT` | `false` | not sent: safe retry |
| Read or write timeout, dropped connection, protocol error | `TIMEOUT` / `NETWORK` | unknown | awaiting reconciliation (`--resend-unknown`) |
| 401 | `AUTHENTICATION` | `true` | rejected, run paused (exit 5) |
| 402, OpenAI 429 `insufficient_quota`, "credit balance is too low" | `INSUFFICIENT_CREDITS` | `true` | rejected, run paused |
| 403 | `PERMISSION_DENIED` | `true` | rejected, run paused |
| 404, `model_not_found` | `MODEL_NOT_FOUND` | `true` | rejected, run paused |
| 400 `context_length_exceeded`, "prompt is too long", 413 | `CONTEXT_TOO_LONG` | `true` | rejected, job failed |
| 3xx (a redirect, never followed) | `INVALID_REQUEST` | `true` | rejected, job failed |
| other 4xx | `INVALID_REQUEST` | `true` | rejected, job failed |
| 429 (rate limit) | `RATE_LIMITED`, with `Retry-After` | `true` | rejected, retried after the wait |
| 529 (Anthropic only: overloaded) | `OVERLOADED` | `true` | rejected, retried |
| 5xx (and 529 from OpenAI) | `SERVER_ERROR` | `true` | unknown billing: reconciliation |
| 2xx that cannot be read, or whose content is not text | `MALFORMED_PROVIDER_RESPONSE` | `true` | unknown billing: reconciliation |

**Any other endpoint** (`openai-compatible`, and `openai` or `anthropic` behind a custom
`base_url`) gives no such guarantee: a gateway can answer 400 or 429 after the upstream
model ran and billed (for example a post-call guardrail). Every HTTP error from it is
`ENDPOINT_ERROR` with billing unknown: the reservation stays charged, the job awaits
reconciliation, and it is sent again only with `--resend-unknown` (and a new authorization).
Consequences: rate limits of such endpoints are not retried automatically, and an account
error (for example a wrong key) does not pause the run; check `llm status`, `error_kind`
in `llm export`, and the `http_status` stored in the `llm_attempts` table. No setting turns these errors into free refusals. Transport
errors keep the rules of the first two rows.

`Retry-After` is read from `retry-after-ms` or `retry-after` (seconds or an HTTP date), capped
at one hour. Safe messages carry only the HTTP status and the provider's error type or code;
the provider's message text is never copied.

## OpenAI-compatible endpoints

Compatibility is declared, never assumed. An endpoint works when it implements
`POST {base_url}/chat/completions` with the OpenAI request and response shapes. Known
limitations:

- Declare `structured_output` for the model; many servers support `json_object` but not
  `json_schema`.
- If the server does not report `usage`, tokens and costs are unknown and budgets use
  reservations.
- HTTP errors never prove that the request was not processed: they are `ENDPOINT_ERROR`
  with unknown billing (see [Errors](#errors)). Rate limits are not retried automatically;
  set `requests_per_minute` and `tokens_per_minute` to the endpoint's limits instead.
- A server without prompt caching should be declared `prompt_caching = false`, otherwise
  costs stay unknown when it reports only `prompt_tokens`.
- There are no built-in prices: add them to `pricing.toml` with `provider = "openai-compatible"`.
- `store` (an OpenAI-only parameter) is not sent.

## Limitations

- Token budgets rely on local estimates; real tokenizers differ (the context and price-tier
  checks use a 1.25 margin, budgets use reservations). Text with many emoji or CJK characters
  can need more tokens than estimated, so those checks are guides, not guarantees.
- `openai` or `anthropic` with a custom `base_url` (a gateway or proxy) is treated like a
  third-party endpoint for error mapping: every HTTP error leaves billing unknown.
- The mapping of `prompt_tokens` assumes it includes cached and written tokens, as the OpenAI
  documentation describes; inconsistent counts become unknown.
- Provider-side idempotency keys are not used; outcomes that cannot be proven stay unknown and
  are never resent without `--resend-unknown`.
- The Responses API of OpenAI is not implemented yet. The adapter boundary (`openai_chat.py`)
  leaves room for an `openai_responses.py` next to it.
