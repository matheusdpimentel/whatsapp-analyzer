"""What every adapter receives, and the usage rules they share."""

from dataclasses import dataclass, field
from typing import Any

from whatsapp_analyzer.llm.catalog import OutputMode
from whatsapp_analyzer.llm.providers import _http
from whatsapp_analyzer.llm.redaction import SecretValue
from whatsapp_analyzer.llm.types import Usage


@dataclass(frozen=True)
class AdapterConfig:
    """Built by `providers.build_provider` after every check passed.

    `identity` names the destination (provider and, for a custom endpoint, its scheme, host,
    port and path). It is the provider name stored with attempts, used for reuse and shown
    in the authorization prompt. Adapters build their HTTP client with `_http.make_client`.
    """

    identity: str
    model: str
    api_key: SecretValue
    base_url: str | None
    output_mode: OutputMode
    options: dict[str, object] = field(default_factory=dict)
    timeout_seconds: float = 120.0
    cache_write_billed: bool | None = None
    prompt_caching: bool | None = None  # False: the endpoint has no prompt cache at all
    official: bool = True  # False for openai-compatible endpoints
    loopback: bool = False
    transport: Any = None  # offline tests only: an httpx2 mock transport

    def __repr__(self) -> str:
        return f"AdapterConfig(identity={self.identity!r}, model={self.model!r})"


def usage_from(
    *,
    input_uncached: int | None,
    cache_read: int | None,
    cache_write: int | None,
    output_total: int | None,
    output_reasoning: int | None,
    cache_write_billed: bool | None,
    prompt_caching: bool | None = None,
) -> Usage:
    """Provider counts -> disjoint buckets. A count that is not reported stays None (unknown);
    a missing cache-write count is zero only when the catalog says the model never bills cache
    writes, and missing cache counts are zero only when it declares no prompt caching.
    Reasoning is part of `output_total` and is dropped (unknown) if it exceeds it."""
    if prompt_caching is False:  # no cache at all: unreported cache counts are known zeros
        cache_read = 0 if cache_read is None else cache_read
        cache_write = 0 if cache_write is None else cache_write
    if cache_write is None and cache_write_billed is False:
        cache_write = 0
    if (
        output_reasoning is not None
        and output_total is not None
        and output_reasoning > output_total
    ):
        output_reasoning = None
    try:
        return Usage(
            input_uncached=input_uncached,
            input_cache_read=cache_read,
            input_cache_write=cache_write,
            output_total=output_total,
            output_reasoning=output_reasoning,
        )
    except ValueError:  # a negative count: the response cannot be trusted
        raise _http.malformed_response() from None
