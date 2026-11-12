"""Registry of real providers and the checks that run before one is built.

The registry is a plain dictionary: adding a provider means writing an adapter that
implements `llm.provider.Provider`, adding one `ProviderSpec` here, and documenting it
(docs/adding-a-provider.md). The runner, the database and the classification never change.

`build_provider` refuses, with a stable code and before anything is sent, every
configuration that cannot work or cannot be checked: unknown options, a temperature the model
does not accept (or is not known to accept), more output tokens than the model allows, an
invalid endpoint, a missing SDK, SDK debug logging (it can print request bodies) and a
missing, empty or malformed API key. Unknown capabilities that a run can do without are
reported as notices instead (for example the structured output mode, which then falls back
to prompt-only JSON checked locally).
"""

import importlib.util
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from whatsapp_analyzer.llm.catalog import ModelInfo, OutputMode
from whatsapp_analyzer.llm.provider import Provider
from whatsapp_analyzer.llm.providers import anthropic_messages, openai_chat
from whatsapp_analyzer.llm.providers.base import AdapterConfig
from whatsapp_analyzer.llm.providers.connection import (
    ProviderSetupError,
    parse_endpoint,
    resolve_api_key,
)

__all__ = [
    "REGISTRY",
    "BuiltProvider",
    "ProviderSetupError",
    "ProviderSpec",
    "build_provider",
    "is_external",
]

# Loggers of the SDKs and their HTTP stack: never below WARNING, whatever the application
# logging configuration says (debug records can contain request options).
_QUIET_LOGGERS = ("openai", "anthropic", "httpx", "httpx2", "httpcore", "httpcore2")


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    package: str | None  # import name of the SDK (also the pip extra); None: no SDK
    log_env: str | None  # SDK variable that turns on debug logging
    default_key_env: str | None  # None: `api_key_env` must be configured
    base_url_required: bool
    options: frozenset[str]
    check_options: Callable[[dict[str, object]], str | None]
    # False when the protocol never accepts a temperature, whatever the model
    temperature_possible: bool
    official: bool
    factory: Callable[[AdapterConfig], Provider]
    # SDK environment variables that would change requests behind the configuration's back
    # (extra headers, even an Authorization header): refused while they are set.
    blocked_env: tuple[str, ...] = ()
    docs: str = "docs/providers.md"


REGISTRY: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        name="openai",
        package="openai",
        log_env="OPENAI_LOG",
        default_key_env="OPENAI_API_KEY",
        base_url_required=False,
        options=openai_chat.OPTIONS - {"token_limit_parameter"},
        check_options=openai_chat.check_options,
        temperature_possible=True,
        official=True,
        factory=openai_chat.OpenAIChatProvider,
        blocked_env=("OPENAI_CUSTOM_HEADERS",),
    ),
    "anthropic": ProviderSpec(
        name="anthropic",
        package="anthropic",
        log_env="ANTHROPIC_LOG",
        default_key_env="ANTHROPIC_API_KEY",
        base_url_required=False,
        options=anthropic_messages.OPTIONS,
        check_options=anthropic_messages.check_options,
        temperature_possible=False,  # API version 2026-10-21 rejects sampling parameters
        official=True,
        factory=anthropic_messages.AnthropicProvider,
        blocked_env=("ANTHROPIC_CUSTOM_HEADERS",),
    ),
    "openai-compatible": ProviderSpec(
        name="openai-compatible",
        package="openai",
        log_env="OPENAI_LOG",
        default_key_env="OPENAI_COMPAT_API_KEY",
        base_url_required=True,
        options=openai_chat.OPTIONS,
        check_options=openai_chat.check_options,
        temperature_possible=True,
        official=False,
        factory=openai_chat.OpenAIChatProvider,
        blocked_env=("OPENAI_CUSTOM_HEADERS",),
    ),
}


def is_external(provider: str) -> bool:
    """True for providers that send content off the process (everything but `fake`)."""
    return provider in REGISTRY


@dataclass(frozen=True)
class BuiltProvider:
    provider: Provider
    identity: str
    base_url: str | None  # the endpoint actually used
    output_mode: OutputMode
    loopback: bool
    notices: tuple[str, ...] = ()
    # provider-specific values that shape requests: part of `request_id` and the run key
    params: tuple[tuple[str, object], ...] = field(default=())


def build_provider(  # noqa: PLR0913
    provider: str,
    model: str,
    *,
    info: ModelInfo,
    max_output_tokens: int,
    temperature: float | None,
    options: tuple[tuple[str, object], ...],
    api_key_env: str | None,
    base_url: str | None,
    timeout_seconds: float,
    environ: Mapping[str, str] | None = None,
    transport: Any = None,
) -> BuiltProvider:
    """`transport` replaces the network with an httpx2 mock transport (offline tests only)."""
    spec = REGISTRY.get(provider)
    if spec is None:
        raise ProviderSetupError("PROVIDER_UNKNOWN", provider=provider)
    chosen = dict(options)
    for name in chosen:
        if name not in spec.options:
            raise ProviderSetupError(
                "OPTION_UNSUPPORTED",
                option=name,
                provider=provider,
                supported=", ".join(sorted(spec.options)) or "-",
            )
    invalid = spec.check_options(chosen)
    if invalid is not None:
        raise ProviderSetupError("OPTION_INVALID", option=invalid, provider=provider)
    notices = _check_capabilities(spec, info, max_output_tokens, temperature)
    mode = info.structured_output or OutputMode.NONE
    if info.structured_output is None:
        notices.append("STRUCTURED_OUTPUT_UNDECLARED")

    endpoint = None
    if base_url is not None:
        endpoint = parse_endpoint(base_url)
    elif spec.base_url_required:
        raise ProviderSetupError("BASE_URL_REQUIRED", provider=provider)
    identity = provider if endpoint is None else f"{provider}@{endpoint.url}"

    env = os.environ if environ is None else environ
    _check_environment(spec, env)
    key_env = api_key_env or spec.default_key_env
    if key_env is None:
        raise ProviderSetupError("API_KEY_ENV_REQUIRED", provider=provider)
    api_key = resolve_api_key(key_env, env)
    for name in _QUIET_LOGGERS:
        logger = logging.getLogger(name)
        if logger.getEffectiveLevel() < logging.WARNING:
            logger.setLevel(logging.WARNING)

    config = AdapterConfig(
        identity=identity,
        model=model,
        api_key=api_key,
        base_url=endpoint.url if endpoint is not None else None,
        output_mode=mode,
        options=chosen,
        timeout_seconds=timeout_seconds,
        cache_write_billed=info.cache_write_billed,
        prompt_caching=info.prompt_caching,
        # a custom base_url (gateway, proxy) is not the official API: its refusals are not trusted
        official=spec.official and endpoint is None,
        loopback=endpoint is not None and endpoint.loopback,
        transport=transport,
    )
    params: list[tuple[str, object]] = [("output_mode", mode.value)]
    params.extend(sorted(chosen.items()))
    return BuiltProvider(
        provider=spec.factory(config),
        identity=identity,
        base_url=config.base_url,
        output_mode=mode,
        loopback=endpoint is not None and endpoint.loopback,
        notices=tuple(notices),
        params=tuple(params),
    )


def _check_environment(spec: ProviderSpec, env: Mapping[str, str]) -> None:
    """The SDK is installed and nothing in the environment changes what it would send."""
    if spec.package is not None and importlib.util.find_spec(spec.package) is None:
        raise ProviderSetupError("PROVIDER_SDK_MISSING", package=spec.package)
    if spec.log_env is not None and env.get(spec.log_env, "").strip().lower() == "debug":
        raise ProviderSetupError("SDK_DEBUG_LOGGING", env=spec.log_env)
    for name in spec.blocked_env:
        if env.get(name):
            raise ProviderSetupError("SDK_ENV_OVERRIDE", env=name)


def _check_capabilities(
    spec: ProviderSpec, info: ModelInfo, max_output_tokens: int, temperature: float | None
) -> list[str]:
    notices: list[str] = []
    if not info.listed:
        notices.append("MODEL_NOT_IN_CATALOG")
    if temperature is not None:
        if not spec.temperature_possible or info.temperature is False:
            raise ProviderSetupError("TEMPERATURE_UNSUPPORTED", model=info.model)
        if info.temperature is None:
            raise ProviderSetupError("TEMPERATURE_UNDECLARED", model=info.model)
    if info.max_output_tokens is not None and max_output_tokens > info.max_output_tokens:
        raise ProviderSetupError(
            "MAX_OUTPUT_EXCEEDS_MODEL",
            model=info.model,
            requested=max_output_tokens,
            maximum=info.max_output_tokens,
        )
    if info.context_window is None:
        notices.append("CONTEXT_WINDOW_UNKNOWN")
    return notices
