"""Anthropic Messages API adapter (official `anthropic` SDK, optional extra).

Request: `system`, one user message, `max_tokens` = `max_output_tokens`, and, when the model
supports it, `output_config.format` with the output JSON Schema. API version 2026-10-21 (the
SDK's) rejects sampling parameters on every model, so `temperature` is never sent; the
provider builder refuses a configured temperature before anything is sent.

Response: the text blocks are joined (thinking blocks are ignored); `stop_reason` maps to
`FinishReason`; `usage.input_tokens` excludes cache reads and writes, so the three input
buckets are disjoint; `output_tokens` already includes reasoning (`thinking_tokens` is part
of it and is never added again).
"""

import json
from typing import Any

from whatsapp_analyzer.llm.catalog import OutputMode
from whatsapp_analyzer.llm.providers import _http
from whatsapp_analyzer.llm.providers.base import AdapterConfig, usage_from
from whatsapp_analyzer.llm.types import (
    ErrorKind,
    FinishReason,
    LLMRequest,
    LLMResult,
    ProviderError,
)

DEFAULT_BASE_URL = "https://api.anthropic.com"
OPTIONS = frozenset({"effort"})
_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})

_FINISH = {
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "model_context_window_exceeded": FinishReason.LENGTH,
    "refusal": FinishReason.REFUSAL,
}


def check_options(options: dict[str, object]) -> str | None:
    """None when valid, otherwise the name of the invalid option."""
    effort = options.get("effort")
    if effort is not None and effort not in _EFFORTS:
        return "effort"
    return None


class AnthropicProvider:
    def __init__(self, config: AdapterConfig) -> None:
        import anthropic  # noqa: PLC0415  (optional dependency, imported only when used)

        self._sdk = anthropic
        self._config = config
        self._client = anthropic.AsyncAnthropic(
            api_key=config.api_key.reveal(),
            base_url=config.base_url or DEFAULT_BASE_URL,
            max_retries=0,  # every attempt is the runner's: never hidden SDK retries
            timeout=config.timeout_seconds,
            open_telemetry=False,  # no tracing of prompts or responses
            http_client=_http.make_client(  # type: ignore[arg-type]
                loopback=config.loopback,
                timeout=config.timeout_seconds,
                transport=config.transport,
            ),
        )

    @property
    def name(self) -> str:
        return self._config.identity

    async def complete(self, request: LLMRequest) -> LLMResult:
        if request.temperature is not None:
            raise ProviderError(
                ErrorKind.INVALID_REQUEST, "temperature is not accepted", request_sent=False
            )
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "system": request.system,
            "messages": [{"role": m.role.value, "content": m.content} for m in request.messages],
        }
        output_config: dict[str, Any] = {}
        if self._config.output_mode is OutputMode.JSON_SCHEMA:
            output_config["format"] = {
                "type": "json_schema",
                "schema": _http.portable_schema(request.response_schema),
            }
        if "effort" in self._config.options:
            output_config["effort"] = self._config.options["effort"]
        if output_config:
            params["output_config"] = output_config
        sdk = self._sdk
        error: ProviderError | None = None
        try:
            message = await self._client.messages.create(**params, timeout=request.timeout_seconds)
        except sdk.APIStatusError as exc:
            error = _http.status_error(exc, self._config.official, overloaded_529=True)
        except (sdk.APIResponseValidationError, json.JSONDecodeError):
            error = _http.malformed_response()  # a 2xx body that cannot be read
        except sdk.APIConnectionError as exc:
            error = _http.connection_error(exc)
        if error is not None:
            # Raised outside the except block: the SDK exception, which holds the request and
            # its headers, is not kept as __context__.
            raise error
        return self._result(message)

    def _result(self, message: Any) -> LLMResult:
        try:
            text = _text(message.content)
            usage = message.usage
            details = getattr(usage, "output_tokens_details", None)
            result_usage = usage_from(
                input_uncached=usage.input_tokens,
                cache_read=usage.cache_read_input_tokens,
                cache_write=usage.cache_creation_input_tokens,
                output_total=usage.output_tokens,
                output_reasoning=getattr(details, "thinking_tokens", None),
                cache_write_billed=self._config.cache_write_billed,
                prompt_caching=self._config.prompt_caching,
            )
            finish = _FINISH.get(message.stop_reason or "", FinishReason.OTHER)
        except (AttributeError, TypeError) as exc:
            raise _http.malformed_response() from exc
        return LLMResult(
            text=text,
            finish_reason=finish,
            usage=result_usage,
            model_reported=getattr(message, "model", None),
            provider_request_id=getattr(message, "_request_id", None),
        )


def _text(content: object) -> str:
    """The text blocks joined; thinking and other blocks are ignored. Raises TypeError (mapped
    to MALFORMED_PROVIDER_RESPONSE) when the content or a text block has the wrong type."""
    if not isinstance(content, list):
        raise TypeError("content is not a list of blocks")
    texts = [getattr(b, "text", None) for b in content if getattr(b, "type", None) == "text"]
    if not all(isinstance(t, str) for t in texts):
        raise TypeError("a text block is not a string")
    return "".join(t for t in texts if isinstance(t, str))
