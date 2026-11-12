"""OpenAI Chat Completions adapter (official `openai` SDK, optional extra).

The same adapter serves the official OpenAI API and OpenAI-compatible endpoints; only the
configuration differs (base URL, declared capabilities, `official`). Compatibility of an
endpoint is declared by the user, never assumed: see docs/providers.md.

Request: a system message and one user message; `max_completion_tokens` (or `max_tokens`
when `token_limit_parameter = "max_tokens"` for an endpoint that needs it); the output format
from the model's declared `structured_output`; `temperature` only when configured (the
builder checks that the model accepts it). For the official API `store` is set to false.

Response: `prompt_tokens` counts every input token, cached and written ones included, so
`input_uncached = prompt_tokens - cached_tokens - cache_write_tokens`, computed only when both
counts are known; `completion_tokens` already includes `reasoning_tokens`.
"""

import json
from typing import Any

from whatsapp_analyzer.llm.catalog import OutputMode
from whatsapp_analyzer.llm.providers import _http
from whatsapp_analyzer.llm.providers.base import AdapterConfig, usage_from
from whatsapp_analyzer.llm.types import FinishReason, LLMRequest, LLMResult, ProviderError, Usage

DEFAULT_BASE_URL = "https://api.openai.com/v1"
OPTIONS = frozenset({"reasoning_effort", "token_limit_parameter"})
_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
_LIMIT_PARAMETERS = frozenset({"max_completion_tokens", "max_tokens"})
SCHEMA_NAME = "engagement_output"

_FINISH = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "content_filter": FinishReason.CONTENT_FILTER,
}


def check_options(options: dict[str, object]) -> str | None:
    effort = options.get("reasoning_effort")
    if effort is not None and effort not in _EFFORTS:
        return "reasoning_effort"
    limit = options.get("token_limit_parameter")
    if limit is not None and limit not in _LIMIT_PARAMETERS:
        return "token_limit_parameter"
    return None


class OpenAIChatProvider:
    def __init__(self, config: AdapterConfig) -> None:
        import openai  # noqa: PLC0415  (optional dependency, imported only when used)

        self._sdk = openai
        self._config = config
        # Omit() removes a default header: these are never sent.
        omitted_headers: dict[str, Any] = {
            "OpenAI-Organization": openai.Omit(),
            "OpenAI-Project": openai.Omit(),
        }
        self._client = openai.AsyncOpenAI(
            api_key=config.api_key.reveal(),
            base_url=config.base_url or DEFAULT_BASE_URL,
            max_retries=0,  # every attempt is the runner's: never hidden SDK retries
            timeout=config.timeout_seconds,
            # OPENAI_ORG_ID / OPENAI_PROJECT_ID from the environment are never sent: they would
            # reach third-party endpoints and select an account behind the configuration's back.
            default_headers=omitted_headers,
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
        options = self._config.options
        limit_name = str(options.get("token_limit_parameter", "max_completion_tokens"))
        params: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system},
                *({"role": m.role.value, "content": m.content} for m in request.messages),
            ],
            limit_name: request.max_output_tokens,
        }
        if self._config.official:
            params["store"] = False
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if "reasoning_effort" in options:
            params["reasoning_effort"] = options["reasoning_effort"]
        mode = self._config.output_mode
        if mode is OutputMode.JSON_SCHEMA:
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": SCHEMA_NAME,
                    "schema": _http.portable_schema(request.response_schema),
                    "strict": True,
                },
            }
        elif mode is OutputMode.JSON_OBJECT:
            params["response_format"] = {"type": "json_object"}
        sdk = self._sdk
        error: ProviderError | None = None
        try:
            completion = await self._client.chat.completions.create(
                **params, timeout=request.timeout_seconds
            )
        except sdk.APIStatusError as exc:
            error = _http.status_error(exc, self._config.official)
        except (sdk.APIResponseValidationError, json.JSONDecodeError):
            error = _http.malformed_response()  # a 2xx body that cannot be read
        except sdk.APIConnectionError as exc:
            error = _http.connection_error(exc)
        if error is not None:
            raise error  # outside the except block: the SDK exception is not kept as context
        return self._result(completion)

    def _result(self, completion: Any) -> LLMResult:
        try:
            choice = completion.choices[0]
            message = choice.message
            if getattr(message, "refusal", None):
                finish = FinishReason.REFUSAL
            else:
                finish = _FINISH.get(choice.finish_reason or "", FinishReason.OTHER)
            text = _text(message.content)
            usage = self._usage(getattr(completion, "usage", None))
        except (AttributeError, IndexError, TypeError) as exc:
            raise _http.malformed_response() from exc
        return LLMResult(
            text=text,
            finish_reason=finish,
            usage=usage,
            model_reported=getattr(completion, "model", None),
            provider_request_id=getattr(completion, "_request_id", None),
        )

    def _usage(self, usage: Any) -> Usage:
        if usage is None:  # not reported: unknown, never free
            return usage_from(
                input_uncached=None,
                cache_read=None,
                cache_write=None,
                output_total=None,
                output_reasoning=None,
                cache_write_billed=None,
            )
        prompt = getattr(usage, "prompt_tokens", None)
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        completion_details = getattr(usage, "completion_tokens_details", None)
        cached = getattr(prompt_details, "cached_tokens", None)
        written = getattr(prompt_details, "cache_write_tokens", None)
        if self._config.prompt_caching is False:  # no cache: unreported counts are zeros
            cached = 0 if cached is None else cached
            written = 0 if written is None else written
        if written is None and self._config.cache_write_billed is False:
            written = 0
        uncached = None
        if prompt is not None and cached is not None and written is not None:
            uncached = prompt - cached - written
            if uncached < 0:  # inconsistent counts: the split is unknown
                uncached, cached, written = None, None, None
        return usage_from(
            input_uncached=uncached,
            cache_read=cached,
            cache_write=written,
            output_total=getattr(usage, "completion_tokens", None),
            output_reasoning=getattr(completion_details, "reasoning_tokens", None),
            cache_write_billed=self._config.cache_write_billed,
            prompt_caching=self._config.prompt_caching,
        )


def _text(content: object) -> str:
    """The SDK does not validate strictly: a number, a list or an object must never reach the
    output validation as "text" (it would crash the whole run). Raises TypeError, which the
    caller maps to MALFORMED_PROVIDER_RESPONSE."""
    if content is None:
        return ""
    if not isinstance(content, str):
        raise TypeError("content is not a string")
    return content
