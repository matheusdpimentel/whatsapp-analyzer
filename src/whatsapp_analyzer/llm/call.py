"""Build a request from a template and a prepared context, run it, and judge the outcome.

`classify` is the unit the milestone 5 runner will schedule: one async call with a timeout,
which maps every outcome onto three separate axes: execution status, classification label
(only when the status is SUCCESS) and security flags (copied from the context, never changing
the label). Cancellation (`asyncio.CancelledError`) is not caught: the runner decides what a
cancelled call means.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from whatsapp_analyzer.hashing import canonical_json, sha256_hex
from whatsapp_analyzer.llm.context import PreparedContext
from whatsapp_analyzer.llm.output import OUTPUT_SCHEMA_ID, output_schema, validate_output
from whatsapp_analyzer.llm.pricing import Price, estimate_cost
from whatsapp_analyzer.llm.prompts import PromptTemplate, RenderedPrompt, render
from whatsapp_analyzer.llm.provider import Provider
from whatsapp_analyzer.llm.telemetry import CallTelemetry
from whatsapp_analyzer.llm.types import (
    ChatMessage,
    Classification,
    ErrorKind,
    ExecutionStatus,
    FinishReason,
    LLMRequest,
    LLMResult,
    ProviderError,
    Role,
    Usage,
    ValidationFailure,
)

DEFAULT_TIMEOUT_SECONDS = 120.0
TEMPERATURE: float | None = None  # provider default; part of request_id


@dataclass(frozen=True)
class Generation:
    """Generation parameters from the configuration. Every value is part of `request_id`, so
    a different value is a different request.

    `params` are provider-specific request parameters (options and the structured output
    mode). They are hashed only when present, so requests without them keep the ids they
    had before real providers existed."""

    max_output_tokens: int | None = None  # None: the prompt template's value
    temperature: float | None = TEMPERATURE
    params: tuple[tuple[str, object], ...] = ()


_FINISH_STATUS = {
    FinishReason.LENGTH: ExecutionStatus.INCOMPLETE_OUTPUT,
    FinishReason.REFUSAL: ExecutionStatus.REFUSED,
    FinishReason.CONTENT_FILTER: ExecutionStatus.CONTENT_FILTERED,
}


@dataclass(frozen=True)
class CallOutcome:
    status: ExecutionStatus
    classification: Classification | None
    telemetry: CallTelemetry


def build_request(
    template: PromptTemplate,
    context: PreparedContext,
    model: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    generation: Generation = Generation(),  # noqa: B008  (frozen, immutable default)
) -> tuple[RenderedPrompt, LLMRequest]:
    rendered = render(
        template,
        {
            "conversation": context.text,
            "message_count": str(context.shown),
            "omitted_count": str(context.omitted),
            "truncated_count": str(context.truncated),
        },
        model,
    )
    max_output = generation.max_output_tokens or template.max_output_tokens
    identity: dict[str, object] = {
        "prompt_hash": rendered.prompt_hash,
        "context_hash": context.context_hash,
        "model": model,
        "output_schema": OUTPUT_SCHEMA_ID,
        # generation parameters: a different value is a different request
        "max_output_tokens": max_output,
        "temperature": generation.temperature,
    }
    if generation.params:
        identity["provider_params"] = dict(generation.params)
    request_id = sha256_hex(canonical_json(identity))
    request = LLMRequest(
        request_id=request_id,
        model=model,
        system=rendered.system,
        messages=(ChatMessage(Role.USER, rendered.user),),
        max_output_tokens=max_output,
        response_schema=output_schema(),
        prompt_id=template.id,
        prompt_version=template.version,
        prompt_hash=rendered.prompt_hash,
        context_hash=context.context_hash,
        temperature=generation.temperature,
        timeout_seconds=timeout_seconds,
    )
    return rendered, request


async def classify(
    provider: Provider,
    request: LLMRequest,
    context: PreparedContext,
    price: Price | None,
    clock: Callable[[], float] = time.monotonic,
) -> CallOutcome:
    started = clock()
    result: LLMResult | None = None
    error: ProviderError | None = None
    try:
        result = await asyncio.wait_for(provider.complete(request), request.timeout_seconds)
    except TimeoutError:
        error = ProviderError(ErrorKind.TIMEOUT, "no response within the timeout")
    except ProviderError as exc:
        error = exc
    except Exception as exc:  # a faulty adapter must still produce an outcome and telemetry
        error = ProviderError(ErrorKind.ADAPTER_ERROR, type(exc).__name__)
    if result is not None and not isinstance(result.text, str):
        # An adapter returned something that is not text: the response arrived (and may be
        # billed), so it is a malformed response of this job, never a crash of the run.
        result = None
        error = ProviderError(
            ErrorKind.MALFORMED_PROVIDER_RESPONSE,
            "response text is not a string",
            request_sent=True,
        )
    latency_ms = round((clock() - started) * 1000)

    classification: Classification | None = None
    validation_error: str | None = None
    if result is None:
        status = ExecutionStatus.FAILED
    elif result.finish_reason in _FINISH_STATUS:
        status = _FINISH_STATUS[result.finish_reason]
    else:
        checked = validate_output(result.text, context.message_ids, context.contact_ids)
        if isinstance(checked, ValidationFailure):
            status = ExecutionStatus.INVALID_RESPONSE
            validation_error = checked.code
        else:
            status, classification = ExecutionStatus.SUCCESS, checked

    usage = result.usage if result is not None else Usage()  # unknown, never zero
    telemetry = CallTelemetry(
        request_id=request.request_id,
        provider=provider.name,
        model=request.model,
        model_reported=result.model_reported if result else None,
        provider_request_id=result.provider_request_id if result else None,
        prompt_id=request.prompt_id,
        prompt_version=request.prompt_version,
        prompt_hash=request.prompt_hash,
        context_hash=request.context_hash,
        status=status,
        label=classification.label if classification else None,
        finish_reason=result.finish_reason if result else None,
        error_kind=error.kind if error else None,
        http_status=error.http_status if error else None,
        retryable=error.retryable if error else None,
        request_sent=error.request_sent if error else None,
        retry_after_seconds=error.retry_after_seconds if error else None,
        validation_error=validation_error,
        security_flags=tuple(sorted(f.value for f in context.security.flags)),
        usage=usage,
        cost=estimate_cost(usage, price),
        latency_ms=latency_ms,
    )
    return CallOutcome(status, classification, telemetry)
