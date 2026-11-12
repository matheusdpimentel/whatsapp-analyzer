# Adding a provider

This guide is for developers who want to support another LLM service (Gemini, Groq, DeepSeek
or anything that comes later). Read it from the top; each step points at real code.

First decide which of three things you need. Only the third one is code:

| You want | What to do |
|---|---|
| A new model of OpenAI or Anthropic | Configuration: `model = "..."`, and an entry in `<workspace>/models.toml` (limits, capabilities) and `<workspace>/pricing.toml` (prices). See [providers.md](providers.md#models-an-open-catalog). |
| A service that speaks the OpenAI Chat Completions protocol (many hosted APIs, Ollama, vLLM, LM Studio, ...) | Configuration: `provider = "openai-compatible"`, `base_url`, and the capabilities you declare. See [providers.md](providers.md#openai-compatible-endpoints). |
| A service with its own protocol | An adapter: follow the steps below. |

Before writing an adapter, check whether the service offers an OpenAI-compatible endpoint
that is good enough: it costs no code and no maintenance.

## 1. The Provider Protocol

`src/whatsapp_analyzer/llm/provider.py`:

```python
class Provider(Protocol):
    @property
    def name(self) -> str: ...
    async def complete(self, request: LLMRequest) -> LLMResult: ...
```

- `name` is the destination identity. It is stored with every attempt, used to reuse results
  (`provider` + `request_id`) and shown in the authorization prompt. Use
  `AdapterConfig.identity`, which the builder computes (it includes the endpoint when a
  `base_url` is configured).
- `complete` sends ONE request. It returns an `LLMResult` or raises `ProviderError`
  (`llm/types.py`). It must never retry, never log content and never suppress
  `asyncio.CancelledError`.
- The runner (`runner/engine.py`) does everything else: checkpoints, retries, budgets,
  reservations, rate limits, reuse, deduplication and reconciliation. An adapter never
  touches the database or the CLI.

## 2. The existing adapters

```
src/whatsapp_analyzer/llm/providers/
    __init__.py            REGISTRY, ProviderSpec, build_provider (all checks before sending)
    base.py                AdapterConfig (what an adapter receives), usage_from (usage rules)
    connection.py          API key from the environment, endpoint validation and identity
    _http.py               shared mapping: HTTP status, transport errors, Retry-After, schema
    openai_chat.py         OpenAI Chat Completions (also openai-compatible)
    anthropic_messages.py  Anthropic Messages API
```

Read `anthropic_messages.py` first: it is about 130 lines and shows the whole pattern.

## 3. Implement an adapter

Create `src/whatsapp_analyzer/llm/providers/<service>_<protocol>.py`. The minimal adapter
below is runnable (it is `tests/example_adapter.py`, used by the test suite); it answers
locally instead of calling a service:

```python
from whatsapp_analyzer.llm.providers import ProviderSpec
from whatsapp_analyzer.llm.providers.base import AdapterConfig, usage_from
from whatsapp_analyzer.llm.tokens import estimate_text_tokens
from whatsapp_analyzer.llm.types import FinishReason, LLMRequest, LLMResult

ANSWER = '{"level": "LOW", "reasons": ["SHORT_ACKNOWLEDGEMENTS_ONLY"], "evidence": []}'


class EchoProvider:
    def __init__(self, config: AdapterConfig) -> None:
        self._config = config  # identity, model, api key, endpoint, options, output mode

    @property
    def name(self) -> str:
        return self._config.identity

    async def complete(self, request: LLMRequest) -> LLMResult:
        prompt = request.system + "".join(m.content for m in request.messages)
        return LLMResult(
            text=ANSWER,
            finish_reason=FinishReason.STOP,
            usage=usage_from(
                input_uncached=estimate_text_tokens(prompt),
                cache_read=0,
                cache_write=0,
                output_total=estimate_text_tokens(ANSWER),
                output_reasoning=None,  # not reported: unknown, never 0
                cache_write_billed=False,
            ),
            model_reported=request.model,
            provider_request_id=None,
        )
```

A real adapter replaces the body of `complete` with one SDK (or HTTP) call. The snippet
below uses a fictional `vendor_sdk`; `anthropic_messages.py` is the same code with a real
SDK. Every `config.` attribute it uses exists on `AdapterConfig` (a test checks this guide):

```python
def __init__(self, config: AdapterConfig) -> None:
    import vendor_sdk  # optional dependency: import it here, not at module level

    self._sdk = vendor_sdk
    self._config = config
    self._client = vendor_sdk.AsyncClient(
        api_key=config.api_key.reveal(),  # SecretValue: never log or repr it
        base_url=config.base_url or DEFAULT_BASE_URL,  # always explicit
        max_retries=0,  # the runner owns every attempt
        timeout=config.timeout_seconds,
        # this project's HTTP client: no redirects, no proxy for loopback endpoints;
        # `config.transport` is a mock transport in offline tests and None in production
        http_client=_http.make_client(
            loopback=config.loopback,
            timeout=config.timeout_seconds,
            transport=config.transport,
        ),
    )


async def complete(self, request: LLMRequest) -> LLMResult:
    error: ProviderError | None = None
    try:
        response = await self._client.generate(**self._params(request))
    except self._sdk.APIStatusError as exc:
        error = _http.status_error(exc, self._config.official)
    except (self._sdk.APIResponseValidationError, json.JSONDecodeError):
        error = _http.malformed_response()
    except self._sdk.APIConnectionError as exc:
        error = _http.connection_error(exc)
    if error is not None:
        raise error  # outside the except block
    return self._result(response)
```

The error is raised outside the `except` block, so the SDK exception (which holds the
request and its headers) is not kept as `__context__`. Turn off anything in the SDK that
records or exports content (for example `open_telemetry=False` in the Anthropic SDK), and
check every environment variable the SDK reads: the key and the URL must always be passed
explicitly, and variables that inject headers must be listed in `ProviderSpec.blocked_env`
(they are refused while set), like `OPENAI_CUSTOM_HEADERS` and `ANTHROPIC_CUSTOM_HEADERS`.

## 4. Register it

Add one entry to `REGISTRY` in `llm/providers/__init__.py`:

```python
"vendor": ProviderSpec(
    name="vendor",                 # the value of `provider =` in analyzer.toml
    package="vendor_sdk",          # import name of the SDK, also the pip extra; None if none
    log_env="VENDOR_LOG",          # SDK debug variable that is refused; None if none
    default_key_env="VENDOR_API_KEY",
    base_url_required=False,
    options=frozenset({"effort"}),
    check_options=vendor_chat.check_options,
    temperature_possible=True,     # False if the protocol never accepts one
    official=True,                 # False: every HTTP error is ENDPOINT_ERROR (billing unknown)
    factory=vendor_chat.VendorProvider,
    blocked_env=("VENDOR_EXTRA_HEADERS",),  # SDK variables that would inject headers
),
```

That is all: `provider = "vendor"` is now valid configuration (the configuration reads the
provider names from the registry), `llm run` builds it, asks for authorization and runs it
with the existing runner. Also add an optional dependency in `pyproject.toml`:

```toml
[project.optional-dependencies]
vendor = ["vendor-sdk>=1.2,<2"]
```

and add the SDK to the `dev` group so the offline tests can import it.

## 5. Declare capabilities

Add verified models to `src/whatsapp_analyzer/llm/models.toml` with `source` (a URL) and
`retrieved_at`. Only list what the provider documents; leave anything else out, because a
missing capability is treated as unknown and never guessed:

```toml
[[models]]
provider = "vendor"
model = "vendor-small-2"
context_window = 128000
max_output_tokens = 8192
temperature = true
structured_output = "json_schema"
cache_write_billed = false
source = "https://docs.vendor.example/models/vendor-small-2"
retrieved_at = 2026-11-10
```

`build_provider` uses them to refuse impossible requests before anything is sent
(temperature, output limit) and the planner to skip prompts that cannot fit.

## 6. Configure models

Users configure your provider like the built-in ones:

```toml
[llm]
provider = "vendor"
model = "vendor-small-2"
[llm.connection]
api_key_env = "VENDOR_API_KEY"
```

A model you did not list still works: it gets the notices `MODEL_NOT_IN_CATALOG` and
`STRUCTURED_OUTPUT_UNDECLARED`, and users can declare it in `<workspace>/models.toml`.

## 7. Credentials

`build_provider` reads the key from the configured variable (or `default_key_env`) with
`connection.resolve_api_key`, refuses missing, empty and malformed values, registers the
variable for redaction and hands the adapter a `SecretValue`. Call `reveal()` only where the
SDK needs the key. Never accept a key from the configuration file, a URL or a command-line
argument.

## 8. Endpoints

If the service has regional or self-hosted endpoints, let users set `base_url`. It is
validated by `connection.parse_endpoint` (HTTPS, HTTP for loopback only, no credentials or
query) and becomes part of the identity (`vendor@https://host:443/path`), so reuse and
authorization never mix endpoints. Pass `config.base_url or DEFAULT_BASE_URL` to the SDK:
never let the SDK read a URL from its own environment variables.

## 9. Requests and responses

From `LLMRequest`, send: `model`, `system`, `messages`, `max_output_tokens` (the provider's
output limit parameter), `temperature` only when it is not `None`, and the output format for
`config.output_mode`:

| `output_mode` | Send |
|---|---|
| `json_schema` | the provider's JSON Schema mode with `_http.portable_schema(request.response_schema)` |
| `json_object` | the provider's "JSON object" mode, if any |
| `none` | nothing: the prompt already asks for JSON |

To `LLMResult`: `text` (the answer text only, without reasoning blocks), `finish_reason`
(`STOP`, `LENGTH`, `REFUSAL`, `CONTENT_FILTER`, otherwise `OTHER`), `usage`,
`model_reported` and `provider_request_id` (the provider's request id header, useful for
support and reconciliation). Never validate the JSON in the adapter: `llm.call.classify` does
it the same way for every provider.

## 10. Usage

Build `Usage` with `base.usage_from`, which enforces the shared rules. The buckets are
disjoint: `input_uncached + input_cache_read + input_cache_write` is the whole input.

- If the provider reports a total input that includes cached tokens (like OpenAI's
  `prompt_tokens`), subtract the cache counts, and only when they are all known.
- A count that is not reported is `None`, never `0`.
- A response without usage is still a billed response: return a `Usage()` with every count
  `None`. The runner charges the reservation, never zero.

## 11. Reasoning and cache tokens

- `output_total` is every billed output token, reasoning included. `output_reasoning` is the
  part spent on reasoning, for information; it is never added again. If the provider reports
  reasoning separately from the output, add it to `output_total` yourself.
- Cache reads and writes go to their own buckets because they are priced differently. A
  missing cache-write count becomes `0` only when the model is declared
  `cache_write_billed = false`.

## 12. Prices

Add an entry to `src/whatsapp_analyzer/llm/prices.toml` for each catalog model, from the
provider's pricing page, with every rate as a decimal string:

```toml
[[prices]]
provider = "vendor"
model = "vendor-small-2"
currency = "USD"
unit = "per_million_tokens"
input = "0.20"
cache_read = "0.02"
cache_write = "0.20"
output = "0.80"
effective_from = 2026-11-10
retrieved_at = 2026-11-10
source = "https://vendor.example/pricing"
```

A complete entry (all four rates) is needed for `--max-cost`. Never invent a price: without
one the cost is reported as unknown. An entry older than 90 days is flagged as stale.

## 13. Errors

Map every failure to an `ErrorKind`. `_http.status_error` already handles statuses and the
documented OpenAI and Anthropic codes; add your provider's documented codes to `_BY_CODE` in
`_http.py` when they refine a status (for example a 429 that means "out of credits").

Map an HTTP error to a "rejected" kind only when the provider DOCUMENTS that the request was
refused before processing. `ProviderSpec.official=True` declares that your adapter talks to
such an API at its default URL; with a custom `base_url`, or for a protocol without that
guarantee, every HTTP error is `ENDPOINT_ERROR` (billing unknown, reconciliation). A
provider-specific status (like Anthropic's 529 "overloaded") is passed by its adapter only
(`status_error(exc, official, overloaded_529=True)`). The important distinctions for an
official API:

| Situation | Kind |
|---|---|
| bad key | `AUTHENTICATION` |
| no credits, quota or billing problem | `INSUFFICIENT_CREDITS` (pauses the run; never `RATE_LIMITED`) |
| no access to the model / unknown model | `PERMISSION_DENIED` / `MODEL_NOT_FOUND` |
| too many requests | `RATE_LIMITED` |
| temporarily overloaded, documented as not processed | `OVERLOADED` |
| prompt too long | `CONTEXT_TOO_LONG` |
| other invalid request | `INVALID_REQUEST` |
| server error | `SERVER_ERROR` (billing unknown) |
| unreadable success response | `MALFORMED_PROVIDER_RESPONSE` |

Safe messages hold the HTTP status and the error type or code only, never the provider's
message text.

## 14. `request_sent`

This decides whether the runner may send again without asking:

- `False` only when you can prove the request never left the machine: the connection was
  never established. `_http.connection_error` detects this from the transport exception
  (`ConnectError`, `ConnectTimeout`, `PoolTimeout`, `UnsupportedProtocol`).
- `True` when a response arrived (any HTTP status).
- `None` (unknown) for everything else: read and write timeouts, dropped connections,
  protocol errors. When in doubt, use `None`: the job then waits for reconciliation instead
  of risking a second charge.

## 15. Retry-After

`_http.retry_after_seconds(headers)` reads `retry-after-ms` and `retry-after` (seconds or
an HTTP date). `status_error` already fills `retry_after_seconds`; if your provider uses
another header, read it there. The runner waits at least that long before the retry.

## 16. The runner

Nothing to do: `llm run` calls `build_provider`, then the M5 runner calls
`provider.complete` for each job. Check the result with `llm status` and `llm export`, and
the attempts in the `llm_attempts` table (provider identity, billing, usage, cost).

## 17. Offline tests

Tests never touch the network (`pytest-socket` blocks it) or a real key. Use the SDK's
`transport` hook of `build_provider` with a mock transport, as `tests/test_providers.py`
does; the adapter still builds its own client with `_http.make_client`, so redirects and
proxies are tested as in production:

```python
transport = Transport(lambda request: httpx2.Response(200, json=vendor_body()))
built = build_provider(
    "vendor",
    "vendor-small-2",
    info=...,
    max_output_tokens=400,
    temperature=None,
    options=(),
    api_key_env="TEST_LLM_KEY",
    base_url=None,
    timeout_seconds=5,
    environ={"TEST_LLM_KEY": "sk-test-..."},
    transport=transport.client(),
)
```

Cover at least: the request body and headers; a valid response; usage with and without
cache and reasoning counts; a response without usage; each finish reason; every documented
error status and code; `Retry-After`; connect errors (`request_sent=False`) versus read
timeouts (`None`); an unreadable 2xx body; cancellation; exactly one HTTP request per call (no
hidden retries); and that the key never appears in errors. `tests/test_provider_runs.py` shows
end-to-end runs through `llm run` with authorization.

## 18. Compatibility check

Before opening a pull request:

```bash
uv run pytest            # offline, all Python versions you can (3.11 to 3.13)
uv run ruff check && uv run ruff format --check
uv run mypy              # strict
```

Optionally, with your own key and explicit authorization, run the opt-in live test
(`tests/live/README.md`), which sends one synthetic conversation with a hard cost limit.
Never add a test that calls a paid API by default.

## 19. Documentation and examples

- Add the provider to the table and the options, tokens and errors sections of
  [providers.md](providers.md).
- Add it to the provider list in the README.
- Add an `analyzer.toml` example and the install command for its extra.

## 20. Pull requests

- One provider per pull request, with its adapter, registry entry, catalog and price entries
  (with sources), optional dependency, offline tests and documentation.
- No real keys, real conversations or recorded real responses in the repository; build test
  bodies from the provider's documented shapes.
- Explain how errors map to `ErrorKind` and how you decided `request_sent`, with links to
  the provider's documentation.
- Do not change the runner, the database schema or the classification: if your provider
  seems to need it, open an issue first.
