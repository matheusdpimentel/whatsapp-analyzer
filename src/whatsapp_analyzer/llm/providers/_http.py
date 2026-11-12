"""Mapping shared by the SDK adapters: HTTP errors, transport errors and Retry-After.

Both official SDKs raise the same three families of errors, which are mapped here without
importing either SDK:

- a status error (`APIStatusError`: an HTTP response arrived) -> an `ErrorKind` from the status
  and the documented error type or code. The request was sent; whether it was billed is
  decided by the runner (`runner.model.judge`) from the kind, never assumed here. Only the
  official APIs get "rejected" kinds; any other endpoint gets ENDPOINT_ERROR (billing unknown);
- a connection error (`APIConnectionError`, `APITimeoutError`) -> `request_sent=False` ONLY when
  the underlying transport error proves the connection was never established (connect error,
  connect timeout, waiting for a pooled connection, unsupported scheme). Read or write
  timeouts, dropped connections and protocol errors keep `request_sent=None` (unknown);
- a 2xx response the SDK cannot parse (`APIResponseValidationError`) ->
  MALFORMED_PROVIDER_RESPONSE with `request_sent=True`: it was processed and possibly billed.

Safe messages contain the HTTP status and the provider's error type or code only. Provider
error messages, request bodies, headers and keys are never copied.

`make_client` builds the HTTP client every adapter must use: redirects are never followed
(a 3xx would otherwise re-send the prompt to a host nobody validated or authorized; it is
reported as a refused request instead), and for a loopback endpoint the environment's and
the system's proxy settings are ignored, so "loopback" really stays on this machine.
"""

import email.utils
import time
from collections.abc import Mapping
from datetime import UTC

from whatsapp_analyzer.llm.types import ErrorKind, ProviderError

# Transport errors that prove the request never left the machine.
_NOT_SENT = frozenset({"ConnectError", "ConnectTimeout", "PoolTimeout", "UnsupportedProtocol"})
_TIMEOUTS = frozenset({"ConnectTimeout", "PoolTimeout", "ReadTimeout", "WriteTimeout"})
_MAX_RETRY_AFTER = 3600.0

_BY_STATUS = {
    401: ErrorKind.AUTHENTICATION,
    402: ErrorKind.INSUFFICIENT_CREDITS,
    403: ErrorKind.PERMISSION_DENIED,
    404: ErrorKind.MODEL_NOT_FOUND,
    408: ErrorKind.TIMEOUT,
    413: ErrorKind.CONTEXT_TOO_LONG,
    429: ErrorKind.RATE_LIMITED,
}
# Documented error codes or types that refine the status.
_BY_CODE = {
    "insufficient_quota": ErrorKind.INSUFFICIENT_CREDITS,  # OpenAI: a 429 that is NOT a rate limit
    "billing_error": ErrorKind.INSUFFICIENT_CREDITS,  # Anthropic 402
    "context_length_exceeded": ErrorKind.CONTEXT_TOO_LONG,  # OpenAI 400
    "model_not_found": ErrorKind.MODEL_NOT_FOUND,  # OpenAI 404
    "request_too_large": ErrorKind.CONTEXT_TOO_LONG,  # Anthropic 413
}
# Lower-case fragments of documented messages that have no stable code yet.
_BY_MESSAGE = (
    ("credit balance is too low", ErrorKind.INSUFFICIENT_CREDITS),
    ("prompt is too long", ErrorKind.CONTEXT_TOO_LONG),
    ("maximum context length", ErrorKind.CONTEXT_TOO_LONG),
)


def make_client(*, loopback: bool, timeout: float, transport: object = None) -> object:
    """The `httpx2.AsyncClient` for an SDK (`transport` is for offline tests only)."""
    import httpx2  # noqa: PLC0415  (installed with either SDK, imported only when used)

    return httpx2.AsyncClient(
        follow_redirects=False,
        trust_env=not loopback,
        timeout=httpx2.Timeout(timeout, connect=min(timeout, 10.0)),
        limits=httpx2.Limits(max_connections=100, max_keepalive_connections=20),
        transport=transport,  # type: ignore[arg-type]
    )


def kind_for_status(  # noqa: PLR0911  (one return per documented rule)
    status: int,
    error_type: str | None,
    error_code: str | None,
    message: str | None,
    official: bool = True,
    overloaded_529: bool = False,
) -> ErrorKind:
    """The kind of an HTTP error.

    Only the official APIs document which errors are refused before processing, so only
    their statuses and codes are mapped to "rejected" kinds. For any other endpoint every
    HTTP error is ENDPOINT_ERROR: billing unknown, whatever the status. `overloaded_529` is
    Anthropic's documented "overloaded, not processed" status; no other protocol gets it.
    """
    if not official:
        return ErrorKind.ENDPOINT_ERROR
    if 300 <= status < 400:  # a redirect that was not followed: the official API did not run
        return ErrorKind.INVALID_REQUEST
    if status == 529:
        return ErrorKind.OVERLOADED if overloaded_529 else ErrorKind.SERVER_ERROR
    for key in (error_code, error_type):
        if key and key in _BY_CODE:
            return _BY_CODE[key]
    lowered = (message or "").lower()
    for fragment, kind in _BY_MESSAGE:
        if fragment in lowered:
            return kind
    if status in _BY_STATUS:
        return _BY_STATUS[status]
    if 400 <= status < 500:
        return ErrorKind.INVALID_REQUEST
    return ErrorKind.SERVER_ERROR  # 5xx and anything else: may have been processed


def retry_after_seconds(headers: Mapping[str, str], now: float | None = None) -> float | None:
    """Seconds to wait from `retry-after-ms`, or `retry-after` (seconds or an HTTP date)."""
    value = headers.get("retry-after-ms")
    if value is not None:
        try:
            return _clamp(float(value) / 1000)
        except ValueError:
            pass
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return _clamp(float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return _clamp(when.timestamp() - (time.time() if now is None else now))


def _clamp(seconds: float) -> float | None:
    if seconds != seconds:  # NaN  # noqa: PLR0124
        return None
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER)


def status_error(
    exc: Exception, official: bool = True, overloaded_529: bool = False
) -> ProviderError:
    """`exc` is an SDK `APIStatusError`: it has `status_code` and `response`."""
    status = int(getattr(exc, "status_code", 0) or 0)
    error_type = _text(getattr(exc, "type", None))
    error_code = _text(getattr(exc, "error_code", None)) or _text(getattr(exc, "code", None))
    message = _text(getattr(exc, "api_message", None)) or _text(getattr(exc, "message", None))
    kind = kind_for_status(status, error_type, error_code, message, official, overloaded_529)
    if 300 <= status < 400:
        error_code = error_code or "REDIRECT_NOT_FOLLOWED"
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    label = "/".join(p for p in (error_type, error_code) if p) or "error"
    return ProviderError(
        kind,
        f"HTTP {status} {label}",
        http_status=status,
        provider_code=error_code or error_type,
        retry_after_seconds=retry_after_seconds(headers),
        request_sent=True,
    )


def connection_error(exc: Exception) -> ProviderError:
    """`exc` is an SDK `APIConnectionError` (or its `APITimeoutError` subclass). The transport
    error it wraps decides whether the request may have been sent."""
    names = {type(cause).__name__ for cause in _causes(exc)} | {
        base.__name__ for cause in _causes(exc) for base in type(cause).__mro__
    }
    never_sent = bool(names & _NOT_SENT)
    if names & _TIMEOUTS or type(exc).__name__ == "APITimeoutError":
        kind = ErrorKind.TIMEOUT
    else:
        kind = ErrorKind.NETWORK
    detail = next((type(c).__name__ for c in _causes(exc) if c is not exc), type(exc).__name__)
    return ProviderError(kind, detail, request_sent=False if never_sent else None)


def malformed_response() -> ProviderError:
    return ProviderError(
        ErrorKind.MALFORMED_PROVIDER_RESPONSE,
        "the provider returned a response that could not be read",
        request_sent=True,
    )


def _causes(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain and len(chain) < 10:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def portable_schema(schema: Mapping[str, object]) -> dict[str, object]:
    """The output schema without keywords that provider structured-output modes commonly
    reject (`uniqueItems`, `maxItems`, `pattern`, `minItems` above 1). Local validation still
    enforces every rule of the full schema, so nothing is lost; the provider only gets a
    looser guide."""

    def strip(value: object) -> object:
        if isinstance(value, Mapping):
            result: dict[str, object] = {}
            for key, item in value.items():
                if key in {"uniqueItems", "maxItems", "pattern"}:
                    continue
                if key == "minItems" and isinstance(item, int) and item > 1:
                    continue
                result[key] = strip(item)
            return result
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    stripped = strip(schema)
    assert isinstance(stripped, dict)
    return stripped
