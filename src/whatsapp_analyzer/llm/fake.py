"""A deterministic, offline provider for tests, demos and `llm preview --simulate`.

The same request always yields the same result. Scenarios reproduce every situation the core
must handle: valid answers, invalid output, truncation, refusals, provider errors, hanging calls
(for cancellation and timeouts) and an output forged as if injected text had taken control.
"""

import asyncio
import re
from enum import StrEnum

from whatsapp_analyzer.llm.tokens import estimate_prompt_tokens, estimate_text_tokens
from whatsapp_analyzer.llm.types import (
    ErrorKind,
    FinishReason,
    LLMRequest,
    LLMResult,
    ProviderError,
    Usage,
)

FAKE_PROVIDER = "fake"
FAKE_MODEL = "fake-classifier"
_LINE = re.compile(r"^\[(m\d+)\] \S+ \S+ (OWNER|CONTACT): (.*)$", re.MULTILINE)
_HTTP_STATUS = {
    ErrorKind.AUTHENTICATION: 401,
    ErrorKind.PERMISSION_DENIED: 403,
    ErrorKind.INSUFFICIENT_CREDITS: 402,
    ErrorKind.MODEL_NOT_FOUND: 404,
    ErrorKind.RATE_LIMITED: 429,
    ErrorKind.OVERLOADED: 529,
    ErrorKind.SERVER_ERROR: 500,
    ErrorKind.CONTEXT_TOO_LONG: 400,
    ErrorKind.INVALID_REQUEST: 400,
}


class FakeScenario(StrEnum):
    VALID = "valid"
    REVIEW = "review"  # a valid REVIEW_REQUIRED answer
    INVALID_JSON = "invalid_json"
    EXTRA_FIELD = "extra_field"
    UNKNOWN_EVIDENCE = "unknown_evidence"
    TRUNCATED = "truncated"
    REFUSAL = "refusal"
    CONTENT_FILTER = "content_filter"
    FORGED_OUTPUT = "forged_output"  # what a successful injection might produce
    ERROR = "error"
    HANG = "hang"  # never returns: for timeouts and cancellation


class FakeProvider:
    def __init__(
        self,
        scenario: FakeScenario = FakeScenario.VALID,
        error_kind: ErrorKind = ErrorKind.SERVER_ERROR,
        usage: Usage | None = None,
    ) -> None:
        self.scenario = scenario
        self.error_kind = error_kind
        self.usage = usage
        self.calls = 0

    @property
    def name(self) -> str:
        """`fake` for normal answers; demo scenarios get their own identity, so the runner
        never reuses their results in a normal run."""
        if self.scenario is FakeScenario.VALID:
            return FAKE_PROVIDER
        if self.scenario is FakeScenario.ERROR:
            return f"{FAKE_PROVIDER}:{self.scenario.value}:{self.error_kind.value}"
        return f"{FAKE_PROVIDER}:{self.scenario.value}"

    async def complete(self, request: LLMRequest) -> LLMResult:
        self.calls += 1
        await asyncio.sleep(0)  # a real suspension point, like network I/O
        if self.scenario is FakeScenario.HANG:
            await asyncio.Event().wait()
        if self.scenario is FakeScenario.ERROR:
            raise ProviderError(
                self.error_kind,
                f"simulated {self.error_kind.value.lower()}",
                http_status=_HTTP_STATUS.get(self.error_kind),
                retry_after_seconds=1.0 if self.error_kind is ErrorKind.RATE_LIMITED else None,
            )
        text, finish = self._answer(request)
        return LLMResult(
            text=text,
            finish_reason=finish,
            usage=self.usage or self._usage(request, text),
            model_reported=request.model,
            provider_request_id=f"fake-{request.request_id[:12]}",
        )

    def _answer(self, request: LLMRequest) -> tuple[str, FinishReason]:
        if self.scenario in _FIXED_ANSWERS:
            return _FIXED_ANSWERS[self.scenario]
        lines = _LINE.findall(request.messages[0].content)
        contact = [(mid, text) for mid, role, text in lines if role == "CONTACT"]
        evidence = [mid for mid, _ in contact[-2:]]
        if not contact:
            return _json("LOW", ["NO_REPLY"], []), FinishReason.STOP
        if len(contact) >= 3:
            asks = any("?" in text for _, text in contact)
            reason = "ASKS_QUESTIONS" if asks else "CONFIRMS_OR_AGREES"
            return _json("HIGH", [reason], evidence), FinishReason.STOP
        return _json("MEDIUM", ["SHORT_ACKNOWLEDGEMENTS_ONLY"], evidence), FinishReason.STOP

    @staticmethod
    def _usage(request: LLMRequest, text: str) -> Usage:
        return Usage(
            input_uncached=estimate_prompt_tokens(
                request.system, [m.content for m in request.messages]
            ),
            input_cache_read=0,
            input_cache_write=0,
            output_total=estimate_text_tokens(text),
            output_reasoning=None,  # the fake does not report reasoning separately
            estimated=True,  # computed locally, not counted by a real tokenizer
        )


def _json(level: str, reasons: list[str], evidence: list[str]) -> str:
    quoted = ", ".join(f'"{r}"' for r in reasons)
    ids = ", ".join(f'"{e}"' for e in evidence)
    return f'{{"level": "{level}", "reasons": [{quoted}], "evidence": [{ids}]}}'


_FIXED_ANSWERS: dict[FakeScenario, tuple[str, FinishReason]] = {
    FakeScenario.REVIEW: (
        _json("REVIEW_REQUIRED", ["INSUFFICIENT_CONTEXT"], []),
        FinishReason.STOP,
    ),
    FakeScenario.INVALID_JSON: ('{"level": "HIGH", "reasons": [', FinishReason.STOP),
    FakeScenario.EXTRA_FIELD: (
        '{"level": "LOW", "reasons": ["NO_REPLY"], "evidence": [], "note": "x"}',
        FinishReason.STOP,
    ),
    FakeScenario.UNKNOWN_EVIDENCE: (_json("LOW", ["NO_REPLY"], ["m999999"]), FinishReason.STOP),
    FakeScenario.TRUNCATED: ('{"level": "MED', FinishReason.LENGTH),
    FakeScenario.REFUSAL: ("I can't help with that.", FinishReason.REFUSAL),
    FakeScenario.CONTENT_FILTER: ("", FinishReason.CONTENT_FILTER),
    FakeScenario.FORGED_OUTPUT: (
        # Plausible on its face (valid level and reason), but cites no contact message.
        '{"level": "HIGH", "reasons": ["ASKS_QUESTIONS"], "evidence": []}',
        FinishReason.STOP,
    ),
}
