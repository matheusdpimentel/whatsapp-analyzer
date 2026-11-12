"""Real provider adapters, offline: every HTTP exchange goes through `httpx2.MockTransport`.

No test opens a socket (pytest-socket is active) and no key is real.
"""

import asyncio
import json
import pickle
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from test_llm_call import CONTEXT
from whatsapp_analyzer.config import ConfigError
from whatsapp_analyzer.llm.call import Generation, build_request, classify
from whatsapp_analyzer.llm.catalog import (
    Catalog,
    ModelInfo,
    OutputMode,
    builtin_catalog,
    load_catalog,
    parse_catalog,
)
from whatsapp_analyzer.llm.output import output_schema
from whatsapp_analyzer.llm.pricing import builtin_prices
from whatsapp_analyzer.llm.prompts import find_template
from whatsapp_analyzer.llm.providers import (
    REGISTRY,
    BuiltProvider,
    ProviderSetupError,
    _http,
    build_provider,
)
from whatsapp_analyzer.llm.providers.connection import parse_endpoint, resolve_api_key
from whatsapp_analyzer.llm.redaction import SecretValue, redact
from whatsapp_analyzer.llm.types import (
    ErrorKind,
    ExecutionStatus,
    FinishReason,
    LLMRequest,
    ProviderError,
)

KEY = "sk-test-SENTINEL-do-not-leak-0123456789"
ENV = {"TEST_LLM_KEY": KEY}
TEMPLATE_MAX = find_template(None).max_output_tokens
VALID = '{"level": "LOW", "reasons": ["SHORT_ACKNOWLEDGEMENTS_ONLY"], "evidence": []}'
Handler = Callable[[httpx2.Request], httpx2.Response]


class Transport:
    """Records every request and answers with `handler`."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.handler(request)

    def client(self) -> httpx2.MockTransport:
        """The transport given to `build_provider`: the adapter still builds its own client."""
        return httpx2.MockTransport(self)

    def body(self, index: int = -1) -> dict[str, Any]:
        data = json.loads(self.requests[index].content)
        assert isinstance(data, dict)
        return data


def anthropic_body(
    text: str = VALID, stop: str = "end_turn", usage: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage
        or {
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens_details": {"thinking_tokens": 10},
        },
    }


def openai_body(
    text: str | None = VALID,
    finish: str = "stop",
    usage: dict[str, Any] | None = None,
    refusal: str | None = None,
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-6-luna",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text, "refusal": refusal},
                "finish_reason": finish,
                "logprobs": None,
            }
        ],
        "usage": usage,
    }


OPENAI_USAGE = {
    "prompt_tokens": 200,
    "completion_tokens": 40,
    "total_tokens": 240,
    "prompt_tokens_details": {"cached_tokens": 50, "cache_write_tokens": 20},
    "completion_tokens_details": {"reasoning_tokens": 15},
}


def reply(
    body: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None
) -> Handler:
    return lambda request: httpx2.Response(status, json=body, headers=headers or {})


def build(
    provider: str,
    transport: Transport | None = None,
    *,
    model: str | None = None,
    info: ModelInfo | None = None,
    temperature: float | None = None,
    options: tuple[tuple[str, object], ...] = (),
    base_url: str | None = None,
    api_key_env: str | None = "TEST_LLM_KEY",
    environ: dict[str, str] | None = None,
    max_output_tokens: int = 400,
) -> BuiltProvider:
    model = model or {"anthropic": "claude-haiku-5-5", "openai": "gpt-6-luna"}.get(
        provider, "custom-model"
    )
    return build_provider(
        provider,
        model,
        info=info or builtin_catalog().lookup(provider, model),
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        options=options,
        api_key_env=api_key_env,
        base_url=base_url,
        timeout_seconds=5,
        environ=ENV if environ is None else environ,
        transport=transport.client() if transport else None,
    )


def request_for(model: str, temperature: float | None = None) -> LLMRequest:
    generation = Generation(temperature=temperature)
    return build_request(find_template(None), CONTEXT, model, 5.0, generation)[1]


def complete(built: BuiltProvider, model: str, temperature: float | None = None) -> Any:
    return asyncio.run(built.provider.complete(request_for(model, temperature)))


def failure(built: BuiltProvider, model: str) -> ProviderError:
    with pytest.raises(ProviderError) as info:
        complete(built, model)
    return info.value


# Anthropic -------------------------------------------------------------------------------


def test_anthropic_request_and_result_mapping() -> None:
    transport = Transport(reply(anthropic_body(), headers={"request-id": "req_123"}))
    built = build("anthropic", transport)
    result = complete(built, "claude-haiku-5-5")

    request = transport.requests[0]
    assert request.url == "https://api.anthropic.com/v1/messages"
    assert request.headers["authorization"] == f"Bearer {KEY}"  # SDK 2.x: bearer auth
    assert "anthropic-version" in request.headers
    body = transport.body()
    assert body["model"] == "claude-haiku-5-5" and body["max_tokens"] == TEMPLATE_MAX
    assert body["system"] and body["messages"][0]["role"] == "user"
    assert "temperature" not in body
    schema = body["output_config"]["format"]["schema"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "uniqueItems" not in json.dumps(schema) and schema["additionalProperties"] is False

    assert result.text == VALID and result.finish_reason is FinishReason.STOP
    assert result.provider_request_id == "req_123"
    assert result.model_reported == "claude-haiku-5-5"
    usage = result.usage
    assert (usage.input_uncached, usage.input_cache_read, usage.input_cache_write) == (120, 0, 0)
    assert (usage.output_total, usage.output_reasoning) == (30, 10)  # reasoning is inside total
    assert usage.estimated is False
    assert built.identity == "anthropic" and built.provider.name == "anthropic"


@pytest.mark.parametrize(
    ("stop", "expected"),
    [
        ("max_tokens", FinishReason.LENGTH),
        ("model_context_window_exceeded", FinishReason.LENGTH),
        ("refusal", FinishReason.REFUSAL),
        ("stop_sequence", FinishReason.STOP),
        ("pause_turn", FinishReason.OTHER),
    ],
)
def test_anthropic_stop_reasons(stop: str, expected: FinishReason) -> None:
    built = build("anthropic", Transport(reply(anthropic_body(stop=stop))))
    assert complete(built, "claude-haiku-5-5").finish_reason is expected


def test_anthropic_ignores_thinking_blocks_and_effort_option_is_sent() -> None:
    body = anthropic_body()
    body["content"] = [{"type": "thinking", "thinking": "...", "signature": "s"}, *body["content"]]
    transport = Transport(reply(body))
    built = build("anthropic", transport, options=(("effort", "low"),))
    assert complete(built, "claude-haiku-5-5").text == VALID
    assert transport.body()["output_config"]["effort"] == "low"
    assert ("effort", "low") in built.params


# OpenAI ----------------------------------------------------------------------------------


def test_openai_request_and_result_mapping() -> None:
    transport = Transport(
        reply(openai_body(usage=OPENAI_USAGE), headers={"x-request-id": "req_oai"})
    )
    built = build("openai", transport)
    result = complete(built, "gpt-6-luna")

    request = transport.requests[0]
    assert request.url == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = transport.body()
    assert body["max_completion_tokens"] == TEMPLATE_MAX and "max_tokens" not in body
    assert body["store"] is False and "temperature" not in body
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True

    assert result.text == VALID and result.provider_request_id == "req_oai"
    usage = result.usage
    # prompt_tokens includes cached and written tokens: the buckets are disjoint
    assert (usage.input_uncached, usage.input_cache_read, usage.input_cache_write) == (130, 50, 20)
    assert usage.input_total == 200
    assert (usage.output_total, usage.output_reasoning) == (40, 15)


def test_openai_missing_cache_write_is_unknown_unless_declared_unbilled() -> None:
    usage = {**OPENAI_USAGE, "prompt_tokens_details": {"cached_tokens": 50}}
    built = build("openai", Transport(reply(openai_body(usage=usage))))
    unknown = complete(built, "gpt-6-luna").usage
    assert unknown.input_cache_write is None and unknown.input_uncached is None
    assert unknown.input_cache_read == 50

    info = ModelInfo("openai", "older-model", cache_write_billed=False, source="test", origin="t")
    built = build(
        "openai", Transport(reply(openai_body(usage=usage))), model="older-model", info=info
    )
    known = complete(built, "older-model").usage
    assert (known.input_uncached, known.input_cache_read, known.input_cache_write) == (150, 50, 0)


def test_openai_response_without_usage_is_unknown_never_free() -> None:
    built = build("openai", Transport(reply(openai_body(usage=None))))
    result = complete(built, "gpt-6-luna")
    assert result.usage.to_dict()["input_total"] is None and result.usage.output_total is None
    price = builtin_prices().lookup("openai", "gpt-6-luna", __import__("datetime").date.today())
    outcome = asyncio.run(classify(built.provider, request_for("gpt-6-luna"), CONTEXT, price))
    assert outcome.status is ExecutionStatus.SUCCESS
    assert outcome.telemetry.cost.total is None  # unknown, not zero


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (openai_body(text=None, refusal="I can't help"), FinishReason.REFUSAL),
        (openai_body(finish="length"), FinishReason.LENGTH),
        (openai_body(finish="content_filter"), FinishReason.CONTENT_FILTER),
        (openai_body(finish="tool_calls"), FinishReason.OTHER),
    ],
)
def test_openai_finish_reasons(body: dict[str, Any], expected: FinishReason) -> None:
    built = build("openai", Transport(reply(body)))
    assert complete(built, "gpt-6-luna").finish_reason is expected


def test_openai_inconsistent_usage_split_becomes_unknown() -> None:
    usage = {**OPENAI_USAGE, "prompt_tokens": 10}  # fewer than cached + written
    built = build("openai", Transport(reply(openai_body(usage=usage))))
    result = complete(built, "gpt-6-luna").usage
    assert result.input_uncached is None and result.input_cache_read is None


def test_reasoning_above_output_total_is_dropped() -> None:
    usage = {**OPENAI_USAGE, "completion_tokens_details": {"reasoning_tokens": 99}}
    built = build("openai", Transport(reply(openai_body(usage=usage))))
    result = complete(built, "gpt-6-luna").usage
    assert result.output_total == 40 and result.output_reasoning is None


def test_invalid_json_text_is_an_invalid_response_after_billing() -> None:
    built = build("openai", Transport(reply(openai_body(text="not json", usage=OPENAI_USAGE))))
    outcome = asyncio.run(classify(built.provider, request_for("gpt-6-luna"), CONTEXT, None))
    assert outcome.status is ExecutionStatus.INVALID_RESPONSE
    assert outcome.telemetry.error_kind is None  # a response came back: billed, not an error


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"<html>oops", "text/html"),
        (b"{not json", "application/json"),
        (b'{"unexpected": 1}', "application/json"),
    ],
)
def test_unreadable_success_body_is_malformed_and_was_sent(
    provider: str, content: bytes, content_type: str
) -> None:
    transport = Transport(
        lambda r: httpx2.Response(200, content=content, headers={"content-type": content_type})
    )
    error = failure(build(provider, transport), _model(provider))
    assert error.kind is ErrorKind.MALFORMED_PROVIDER_RESPONSE
    assert error.request_sent is True  # processed and possibly billed: never "safe to resend"
    assert len(transport.requests) == 1


def _model(provider: str) -> str:
    return {"anthropic": "claude-haiku-5-5", "openai": "gpt-6-luna"}[provider]


# Errors ----------------------------------------------------------------------------------


def error_body(provider: str, kind: str, message: str = "nope", code: str | None = None) -> Any:
    if provider == "anthropic":
        return {"type": "error", "error": {"type": kind, "message": message}}
    return {"error": {"message": message, "type": kind, "code": code or kind, "param": None}}


@pytest.mark.parametrize(
    ("provider", "status", "kind", "code", "message", "expected"),
    [
        ("anthropic", 401, "authentication_error", None, "bad key", ErrorKind.AUTHENTICATION),
        ("anthropic", 402, "billing_error", None, "pay", ErrorKind.INSUFFICIENT_CREDITS),
        (
            "anthropic",
            400,
            "invalid_request_error",
            None,
            "Your credit balance is too low",
            ErrorKind.INSUFFICIENT_CREDITS,
        ),
        ("anthropic", 403, "permission_error", None, "no", ErrorKind.PERMISSION_DENIED),
        ("anthropic", 404, "not_found_error", None, "model: x", ErrorKind.MODEL_NOT_FOUND),
        (
            "anthropic",
            400,
            "invalid_request_error",
            None,
            "prompt is too long: 300000 tokens",
            ErrorKind.CONTEXT_TOO_LONG,
        ),
        ("anthropic", 413, "request_too_large", None, "big", ErrorKind.CONTEXT_TOO_LONG),
        ("anthropic", 400, "invalid_request_error", None, "bad", ErrorKind.INVALID_REQUEST),
        ("anthropic", 429, "rate_limit_error", None, "slow down", ErrorKind.RATE_LIMITED),
        ("anthropic", 529, "overloaded_error", None, "busy", ErrorKind.OVERLOADED),
        ("anthropic", 500, "api_error", None, "oops", ErrorKind.SERVER_ERROR),
        ("openai", 401, "invalid_request_error", "invalid_api_key", "x", ErrorKind.AUTHENTICATION),
        ("openai", 429, "insufficient_quota", None, "Exceeded!", ErrorKind.INSUFFICIENT_CREDITS),
        ("openai", 429, "requests", "rate_limit_exceeded", "rpm", ErrorKind.RATE_LIMITED),
        (
            "openai",
            404,
            "invalid_request_error",
            "model_not_found",
            "Gone!",
            ErrorKind.MODEL_NOT_FOUND,
        ),
        (
            "openai",
            400,
            "invalid_request_error",
            "context_length_exceeded",
            "long",
            ErrorKind.CONTEXT_TOO_LONG,
        ),
        ("openai", 502, "server_error", None, "gateway", ErrorKind.SERVER_ERROR),
        ("openai", 503, "server_error", None, "unavailable", ErrorKind.SERVER_ERROR),
    ],
)
def test_status_errors_are_mapped_once_without_sdk_retries(
    provider: str, status: int, kind: str, code: str | None, message: str, expected: ErrorKind
) -> None:
    transport = Transport(reply(error_body(provider, kind, message, code), status))
    error = failure(build(provider, transport), _model(provider))
    assert error.kind is expected and error.http_status == status
    assert error.request_sent is True
    assert len(transport.requests) == 1  # max_retries=0: the runner owns every attempt
    assert message not in str(error)  # provider messages are never copied


@pytest.mark.parametrize(
    ("headers", "seconds"),
    [({"retry-after": "20"}, 20.0), ({"retry-after-ms": "1500"}, 1.5), ({}, None)],
)
def test_rate_limit_retry_after(headers: dict[str, str], seconds: float | None) -> None:
    transport = Transport(reply(error_body("anthropic", "rate_limit_error"), 429, headers))
    error = failure(build("anthropic", transport), "claude-haiku-5-5")
    assert error.kind is ErrorKind.RATE_LIMITED and error.retry_after_seconds == seconds


def test_retry_after_parsing() -> None:
    assert _http.retry_after_seconds({"retry-after": "-5"}) == 0.0
    assert _http.retry_after_seconds({"retry-after": "99999"}) == 3600.0
    assert _http.retry_after_seconds({"retry-after": "soon"}) is None
    date_header = {"retry-after": "Wed, 21 Oct 2015 07:28:30 GMT"}
    assert _http.retry_after_seconds(date_header, now=1445412480.0) == 30.0


def raising(exc: type[Exception]) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc("simulated", request=request)  # type: ignore[call-arg]

    return handler


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize(
    ("exc", "kind", "sent"),
    [
        (httpx2.ConnectError, ErrorKind.NETWORK, False),  # never connected: not sent
        (httpx2.ConnectTimeout, ErrorKind.TIMEOUT, False),
        (httpx2.PoolTimeout, ErrorKind.TIMEOUT, False),
        (httpx2.ReadTimeout, ErrorKind.TIMEOUT, None),  # sent, outcome unknown
        (httpx2.WriteTimeout, ErrorKind.TIMEOUT, None),
        (httpx2.RemoteProtocolError, ErrorKind.NETWORK, None),
        (httpx2.ReadError, ErrorKind.NETWORK, None),
    ],
)
def test_transport_errors_and_request_sent(
    provider: str, exc: type[Exception], kind: ErrorKind, sent: bool | None
) -> None:
    transport = Transport(raising(exc))
    error = failure(build(provider, transport), _model(provider))
    assert (error.kind, error.request_sent) == (kind, sent)
    assert len(transport.requests) == 1


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_cancellation_propagates(provider: str) -> None:
    started = asyncio.Event()

    async def scenario() -> None:
        async def slow(request: httpx2.Request) -> httpx2.Response:
            started.set()
            await asyncio.sleep(60)
            raise AssertionError("not reached")

        built = build_provider(
            provider,
            _model(provider),
            info=builtin_catalog().lookup(provider, _model(provider)),
            max_output_tokens=400,
            temperature=None,
            options=(),
            api_key_env="TEST_LLM_KEY",
            base_url=None,
            timeout_seconds=5,
            environ=ENV,
            transport=httpx2.MockTransport(slow),
        )
        task = asyncio.create_task(built.provider.complete(request_for(_model(provider))))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


# Configuration checks --------------------------------------------------------------------


def setup_code(**kwargs: Any) -> str:
    provider = kwargs.pop("provider", "anthropic")
    with pytest.raises(ProviderSetupError) as info:
        build(provider, **kwargs)
    return info.value.code


def test_configuration_is_refused_before_anything_is_sent() -> None:
    assert setup_code(options=(("top_k", 5),)) == "OPTION_UNSUPPORTED"
    assert setup_code(options=(("effort", "turbo"),)) == "OPTION_INVALID"
    assert setup_code(temperature=0.0) == "TEMPERATURE_UNSUPPORTED"  # anthropic, any model
    assert setup_code(provider="openai", temperature=0.2) == "TEMPERATURE_UNDECLARED"
    assert setup_code(max_output_tokens=200_000) == "MAX_OUTPUT_EXCEEDS_MODEL"
    assert setup_code(provider="openai-compatible") == "BASE_URL_REQUIRED"
    assert setup_code(environ={}) == "API_KEY_MISSING"
    assert setup_code(environ={"TEST_LLM_KEY": "  "}) == "API_KEY_EMPTY"
    assert setup_code(environ={"TEST_LLM_KEY": "sk-1 2"}) == "API_KEY_MALFORMED"
    assert setup_code(api_key_env="1BAD") == "API_KEY_ENV_INVALID"
    # without api_key_env, each provider reads its documented default variable, nothing else
    with pytest.raises(ProviderSetupError) as info:
        build("openai-compatible", base_url="https://x.example/v1", api_key_env=None)
    assert (info.value.code, info.value.details) == (
        "API_KEY_MISSING",
        {"env": "OPENAI_COMPAT_API_KEY"},
    )
    assert setup_code(environ={**ENV, "ANTHROPIC_LOG": "debug"}) == "SDK_DEBUG_LOGGING"
    assert setup_code(provider="gemini") == "PROVIDER_UNKNOWN"


def test_declared_temperature_is_sent() -> None:
    info = ModelInfo("openai", "gpt-x", temperature=True, source="test", origin="models.toml")
    transport = Transport(reply(openai_body(usage=OPENAI_USAGE)))
    built = build("openai", transport, model="gpt-x", info=info, temperature=0.2)
    complete(built, "gpt-x", temperature=0.2)
    assert transport.body()["temperature"] == 0.2


def test_missing_sdk_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    assert setup_code() == "PROVIDER_SDK_MISSING"


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://api.example.com/v1", "BASE_URL_INSECURE"),
        ("https://user:pass@api.example.com/v1", "BASE_URL_HAS_CREDENTIALS"),
        ("https://api.example.com/v1?key=abc", "BASE_URL_HAS_QUERY"),
        ("https://api.example.com/v1#frag", "BASE_URL_HAS_QUERY"),
        ("ftp://api.example.com", "BASE_URL_INVALID"),
        ("https://", "BASE_URL_INVALID"),
        ("https://api.example.com/a/../b", "BASE_URL_INVALID"),
        ("https://api.example.com:99999/", "BASE_URL_INVALID"),
        ("https://api.example.com/ v1", "BASE_URL_INVALID"),
    ],
)
def test_invalid_endpoints(url: str, code: str) -> None:
    assert setup_code(provider="openai-compatible", base_url=url) == code


def test_endpoint_identity_distinguishes_scheme_host_port_and_path() -> None:
    urls = [
        "https://api.example.com/v1",
        "https://api.example.com:8443/v1",
        "https://api.example.com/v2",
        "https://other.example.com/v1",
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/v1",
    ]
    identities = {build("openai-compatible", base_url=u).identity for u in urls}
    assert len(identities) == len(urls)
    assert build("openai-compatible", base_url="https://API.example.com/v1/").identity == (
        "openai-compatible@https://api.example.com:443/v1"
    )
    loopback = build("openai-compatible", base_url="http://[::1]:11434/v1")
    assert loopback.loopback and loopback.identity == "openai-compatible@http://[::1]:11434/v1"
    assert parse_endpoint("http://localhost:8000").loopback


def test_compatible_endpoint_uses_declared_capabilities() -> None:
    info = ModelInfo(
        "openai-compatible",
        "local-model",
        structured_output=OutputMode.JSON_OBJECT,
        source="test",
        origin="models.toml",
    )
    transport = Transport(reply(openai_body(usage=None)))
    built = build(
        "openai-compatible",
        transport,
        model="local-model",
        info=info,
        base_url="http://localhost:8000/v1",
        options=(("token_limit_parameter", "max_tokens"),),
    )
    complete(built, "local-model")
    body = transport.body()
    assert str(transport.requests[0].url) == "http://localhost:8000/v1/chat/completions"
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == TEMPLATE_MAX and "max_completion_tokens" not in body
    assert "store" not in body  # official-only parameter
    assert ("output_mode", "json_object") in built.params


def test_unknown_model_is_usable_but_reported() -> None:
    transport = Transport(reply(openai_body(usage=OPENAI_USAGE)))
    built = build("openai", transport, model="gpt-from-the-future")
    assert built.output_mode is OutputMode.NONE
    assert {
        "MODEL_NOT_IN_CATALOG",
        "STRUCTURED_OUTPUT_UNDECLARED",
        "CONTEXT_WINDOW_UNKNOWN",
    } <= set(built.notices)
    complete(built, "gpt-from-the-future")
    assert "response_format" not in transport.body()  # JSON Schema support is not assumed


def test_sdk_environment_defaults_never_override_the_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in (
        ("OPENAI_API_KEY", "sk-env-default"),
        ("OPENAI_BASE_URL", "https://elsewhere.example/v1"),
        ("ANTHROPIC_API_KEY", "sk-env-default"),
        ("ANTHROPIC_BASE_URL", "https://elsewhere.example"),
        ("ANTHROPIC_AUTH_TOKEN", "token-default"),
    ):
        monkeypatch.setenv(name, value)
    for provider in ("openai", "anthropic"):
        transport = Transport(
            reply(openai_body(usage=OPENAI_USAGE) if provider == "openai" else anthropic_body())
        )
        complete(build(provider, transport), _model(provider))
        request = transport.requests[0]
        assert "elsewhere" not in str(request.url)
        assert "sk-env-default" not in str(request.headers)
        assert "token-default" not in str(request.headers)


# Secrets ---------------------------------------------------------------------------------


def test_api_key_is_never_shown() -> None:
    key = resolve_api_key("TEST_LLM_KEY", ENV)
    assert KEY not in repr(key) and KEY not in str(key) and key.reveal() == KEY
    with pytest.raises(TypeError):
        pickle.dumps(key)
    built = build("anthropic")
    assert KEY not in repr(built) and KEY not in repr(built.provider.__dict__.get("_config"))


def test_provider_error_echoing_the_key_is_not_copied() -> None:
    message = f"Incorrect API key provided: {KEY}"
    transport = Transport(reply(error_body("openai", "invalid_request_error", message), 401))
    error = failure(build("openai", transport), "gpt-6-luna")
    assert KEY not in str(error) and KEY not in error.safe_message
    assert KEY not in repr(error.__dict__)


def test_setup_errors_carry_names_not_keys() -> None:
    with pytest.raises(ProviderSetupError) as info:
        build("anthropic", environ={"TEST_LLM_KEY": f"{KEY} "})
    assert KEY not in repr(info.value.details) and KEY not in str(info.value)


# Catalog ---------------------------------------------------------------------------------


def test_builtin_catalog_entries_are_sourced() -> None:
    catalog = builtin_catalog()
    for info in catalog.entries.values():
        assert info.source and info.source.startswith("https://") and info.retrieved_at
        assert info.provider in REGISTRY
    assert catalog.lookup("anthropic", "claude-haiku-5-5").temperature is False
    unknown = catalog.lookup("openai", "not-listed")
    assert not unknown.listed and unknown.context_window is None


def test_workspace_catalog_adds_and_overrides_models(tmp_path: Any) -> None:
    path = tmp_path / "models.toml"
    path.write_text(
        "schema = 1\n"
        "[[models]]\n"
        'provider = "openai-compatible"\nmodel = "my-model"\ncontext_window = 32000\n'
        'structured_output = "json_object"\nsource = "my server docs"\n'
        "[[models]]\n"
        'provider = "anthropic"\nmodel = "claude-haiku-5-5"\nmax_output_tokens = 1000\n'
        'source = "override"\n',
        encoding="utf-8",
    )
    catalog = load_catalog(path)
    added = catalog.lookup("openai-compatible", "my-model")
    assert added.listed and added.origin == "models.toml" and added.context_window == 32000
    overridden = catalog.lookup("anthropic", "claude-haiku-5-5")
    assert overridden.max_output_tokens == 1000 and overridden.context_window is None


@pytest.mark.parametrize(
    ("entry", "kind"),
    [
        ({"provider": "openai", "model": "m"}, "missing_key"),
        ({"provider": "openai", "model": "m", "source": "s", "price": "1"}, "unknown_key"),
        (
            {"provider": "openai", "model": "m", "source": "s", "structured_output": "xml"},
            "invalid_choice",
        ),
        ({"provider": "openai", "model": "m", "source": "s", "temperature": "yes"}, "wrong_type"),
        ({"provider": "openai", "model": "m", "source": "s", "context_window": 0}, "out_of_range"),
    ],
)
def test_invalid_catalog_entries(entry: dict[str, Any], kind: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_catalog({"schema": 1, "models": [entry]}, "models.toml")
    assert info.value.kind == kind


def test_duplicate_catalog_entries() -> None:
    entry = {"provider": "openai", "model": "m", "source": "s"}
    with pytest.raises(ConfigError) as info:
        parse_catalog({"schema": 1, "models": [entry, entry]}, "models.toml")
    assert info.value.kind == "duplicate_model"
    assert isinstance(Catalog([]).lookup("x", "y"), ModelInfo)


def test_portable_schema_is_a_looser_copy() -> None:
    original = output_schema()
    portable = _http.portable_schema(original)
    assert original == output_schema()  # not modified
    reasons = portable["properties"]["reasons"]  # type: ignore[index]
    assert "maxItems" not in reasons and "uniqueItems" not in reasons
    assert reasons["minItems"] == 1
    assert "pattern" not in portable["properties"]["evidence"]["items"]  # type: ignore[index]


def test_api_key_requires_explicit_reveal() -> None:
    key = SecretValue("s3cr3t-xyz")
    assert "s3cr3t" not in f"{key}" and key.reveal() == "s3cr3t-xyz"


def test_custom_key_variables_are_redacted_once_read(monkeypatch: pytest.MonkeyPatch) -> None:
    custom = "MY_CUSTOM_LLM_KEY_FOR_TEST"
    monkeypatch.setenv(custom, "custom-secret-value-123")
    resolve_api_key(custom)
    assert "custom-secret-value-123" not in redact("echo custom-secret-value-123")


# Review findings (M6 internal review) ----------------------------------------------------


@pytest.mark.parametrize("provider", ["openai", "anthropic", "openai-compatible"])
def test_redirects_are_never_followed(provider: str) -> None:
    """A 3xx would re-send the prompt to a host nobody validated or authorized."""
    target = "http://collector.example/x"
    transport = Transport(lambda r: httpx2.Response(308, headers={"location": target}))
    base_url = "https://gateway.example/v1" if provider == "openai-compatible" else None
    built = build(provider, transport, base_url=base_url, model=_model_for(provider))
    error = failure(built, _model_for(provider))
    assert [str(r.url.host) for r in transport.requests] != ["collector.example"]
    assert len(transport.requests) == 1  # only the configured endpoint was contacted
    expected = ErrorKind.ENDPOINT_ERROR if base_url else ErrorKind.INVALID_REQUEST
    assert error.kind is expected and error.http_status == 308
    assert error.request_sent is True  # an answer came back: never "not sent"


def _model_for(provider: str) -> str:
    return {"anthropic": "claude-haiku-5-5", "openai": "gpt-6-luna"}.get(provider, "m")


@pytest.mark.parametrize(
    ("provider", "env"),
    [
        ("openai", "OPENAI_CUSTOM_HEADERS"),
        ("openai-compatible", "OPENAI_CUSTOM_HEADERS"),
        ("anthropic", "ANTHROPIC_CUSTOM_HEADERS"),
    ],
)
def test_sdk_header_variables_are_refused(provider: str, env: str) -> None:
    environ = {**ENV, env: "Authorization: Bearer ambient-secret"}
    base_url = "https://third.example/v1" if provider == "openai-compatible" else None
    with pytest.raises(ProviderSetupError) as info:
        build(provider, base_url=base_url, environ=environ)
    assert (info.value.code, info.value.details) == ("SDK_ENV_OVERRIDE", {"env": env})


@pytest.mark.parametrize("provider", ["openai", "openai-compatible"])
def test_openai_organization_and_project_from_the_environment_are_not_sent(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_ORG_ID", "org-ambient")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-ambient")
    transport = Transport(reply(openai_body(usage=OPENAI_USAGE)))
    base_url = "https://third.example/v1" if provider == "openai-compatible" else None
    complete(build(provider, transport, base_url=base_url, model="gpt-6-luna"), "gpt-6-luna")
    headers = transport.requests[0].headers
    assert "openai-organization" not in headers and "openai-project" not in headers


def test_loopback_endpoints_ignore_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.corp.example:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.corp.example:3128")
    local = _http.make_client(loopback=True, timeout=5)
    remote = _http.make_client(loopback=False, timeout=5)
    assert isinstance(local, httpx2.AsyncClient) and isinstance(remote, httpx2.AsyncClient)
    assert local._trust_env is False and remote._trust_env is True
    assert local.follow_redirects is False and remote.follow_redirects is False
    url = httpx2.URL("http://localhost:11434/v1/chat/completions")
    assert local._transport_for_url(url) is local._transport  # no proxy mount


@pytest.mark.parametrize(
    "url",
    [
        "https://ex\u200bample.com/v1",  # zero-width space: displays as example.com
        "https://exam\u00adple.com/v1",  # soft hyphen
        "https://api.example.com\\evil.com/v1",
        "https://b\u00fccher.example/v1",  # use the xn-- form instead
        "https://-bad-.example/v1",
    ],
)
def test_misleading_hosts_are_refused(url: str) -> None:
    assert setup_code(provider="openai-compatible", base_url=url) == "BASE_URL_INVALID"


def test_punycode_and_ipv6_hosts_are_accepted() -> None:
    assert build("openai-compatible", base_url="https://xn--bcher-kva.example/v1").identity == (
        "openai-compatible@https://xn--bcher-kva.example:443/v1"
    )
    assert build("openai-compatible", base_url="https://[2001:db8::1]:8443/v1").identity == (
        "openai-compatible@https://[2001:db8::1]:8443/v1"
    )


@pytest.mark.parametrize(
    ("status", "official", "overloaded_529", "expected"),
    [
        (529, True, True, ErrorKind.OVERLOADED),  # Anthropic documents it as not processed
        (529, True, False, ErrorKind.SERVER_ERROR),  # OpenAI does not
        (409, True, False, ErrorKind.INVALID_REQUEST),
        (307, True, False, ErrorKind.INVALID_REQUEST),
        (400, False, False, ErrorKind.ENDPOINT_ERROR),  # a gateway may have run the model
        (429, False, False, ErrorKind.ENDPOINT_ERROR),
        (401, False, False, ErrorKind.ENDPOINT_ERROR),
        (402, False, False, ErrorKind.ENDPOINT_ERROR),
        (404, False, False, ErrorKind.ENDPOINT_ERROR),
        (529, False, True, ErrorKind.ENDPOINT_ERROR),
        (307, False, False, ErrorKind.ENDPOINT_ERROR),
        (500, False, False, ErrorKind.ENDPOINT_ERROR),
    ],
)
def test_refusals_are_trusted_only_where_documented(
    status: int, official: bool, overloaded_529: bool, expected: ErrorKind
) -> None:
    kind = _http.kind_for_status(status, None, None, None, official, overloaded_529)
    assert kind is expected


def test_no_prompt_cache_makes_missing_cache_counts_known_zeros() -> None:
    usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    info = ModelInfo(
        "openai-compatible", "local", prompt_caching=False, source="test", origin="models.toml"
    )
    built = build(
        "openai-compatible",
        Transport(reply(openai_body(usage=usage))),
        model="local",
        info=info,
        base_url="http://localhost:8000/v1",
    )
    result = complete(built, "local").usage
    assert (result.input_uncached, result.input_cache_read, result.input_cache_write) == (100, 0, 0)
    unknown = build(
        "openai-compatible",
        Transport(reply(openai_body(usage=usage))),
        model="other",
        base_url="http://localhost:8000/v1",
    )
    assert complete(unknown, "other").usage.input_uncached is None  # not declared: unknown


def test_provider_errors_do_not_keep_the_sdk_exception() -> None:
    transport = Transport(reply(error_body("openai", "invalid_request_error"), 401))
    error = failure(build("openai", transport), "gpt-6-luna")
    assert error.__context__ is None and error.__cause__ is None


def test_official_providers_behind_a_custom_base_url_are_not_trusted_like_the_official_api() -> (
    None
):
    body = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    official = failure(build("anthropic", Transport(reply(body, 529))), "claude-haiku-5-5")
    gateway = build("anthropic", Transport(reply(body, 529)), base_url="https://gw.example")
    assert official.kind is ErrorKind.OVERLOADED  # documented by Anthropic as not processed
    assert failure(gateway, "claude-haiku-5-5").kind is ErrorKind.ENDPOINT_ERROR  # unknown


def test_docker_style_host_names_are_accepted() -> None:
    built = build("openai-compatible", base_url="https://my_service:8000/v1")
    assert built.identity == "openai-compatible@https://my_service:8000/v1"


# M6-04: content of an unexpected type ---------------------------------------------------


@pytest.mark.parametrize("content", [12, ["a", "b"], {"text": VALID}, 1.5, True])
def test_openai_content_of_the_wrong_type_is_malformed(content: object) -> None:
    transport = Transport(reply(openai_body(text=content, usage=OPENAI_USAGE)))  # type: ignore[arg-type]
    error = failure(build("openai", transport), "gpt-6-luna")
    assert error.kind is ErrorKind.MALFORMED_PROVIDER_RESPONSE
    assert error.request_sent is True  # a response arrived: billing stays unknown


@pytest.mark.parametrize(
    ("content", "status"),
    [(None, ExecutionStatus.INVALID_RESPONSE), (VALID, ExecutionStatus.SUCCESS)],
)
def test_openai_null_and_string_content(content: str | None, status: ExecutionStatus) -> None:
    built = build("openai", Transport(reply(openai_body(text=content, usage=OPENAI_USAGE))))
    outcome = asyncio.run(classify(built.provider, request_for("gpt-6-luna"), CONTEXT, None))
    assert outcome.status is status  # null content: an empty answer, validated as such


@pytest.mark.parametrize(
    "content",
    [12, "plain string", {"type": "text", "text": VALID}, [{"type": "text", "text": 12}]],
)
def test_anthropic_content_of_the_wrong_type_is_malformed(content: object) -> None:
    body = anthropic_body()
    body["content"] = content
    error = failure(build("anthropic", Transport(reply(body))), "claude-haiku-5-5")
    assert error.kind is ErrorKind.MALFORMED_PROVIDER_RESPONSE and error.request_sent is True


def test_classify_isolates_any_adapter_that_returns_non_text() -> None:
    class Broken:
        name = "broken"

        async def complete(self, request: LLMRequest) -> Any:
            from whatsapp_analyzer.llm.types import LLMResult, Usage  # noqa: PLC0415

            return LLMResult(text=12, finish_reason=FinishReason.STOP, usage=Usage())  # type: ignore[arg-type]

    outcome = asyncio.run(classify(Broken(), request_for("m"), CONTEXT, None))
    assert outcome.status is ExecutionStatus.FAILED
    assert outcome.telemetry.error_kind is ErrorKind.MALFORMED_PROVIDER_RESPONSE
    assert outcome.telemetry.request_sent is True


# M6-05: the adapter guide matches the code -----------------------------------------------


def test_adding_a_provider_guide_uses_only_real_attributes() -> None:
    import dataclasses  # noqa: PLC0415
    import re  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from whatsapp_analyzer.llm.providers import ProviderSpec  # noqa: PLC0415
    from whatsapp_analyzer.llm.providers.base import AdapterConfig  # noqa: PLC0415

    guide = (Path(__file__).parents[1] / "docs" / "adding-a-provider.md").read_text("utf-8")
    config_fields = {f.name for f in dataclasses.fields(AdapterConfig)}
    used = set(re.findall(r"\bconfig\.([a-z_]+)", guide))
    assert used and used <= config_fields, used - config_fields
    helpers = set(re.findall(r"\b_http\.([a-z_]+)\(", guide))
    assert helpers and all(callable(getattr(_http, name, None)) for name in helpers), helpers
    spec_block = guide[guide.index('"vendor": ProviderSpec(') :]
    spec_block = spec_block[: spec_block.index("\n),")]
    spec_keys = set(re.findall(r"^\s+([a-z_]+)=", spec_block, re.MULTILINE))
    assert spec_keys <= {f.name for f in dataclasses.fields(ProviderSpec)}, spec_keys
    for name in re.findall(r"\b(make_client|status_error|connection_error)\(", guide):
        assert hasattr(_http, name)
