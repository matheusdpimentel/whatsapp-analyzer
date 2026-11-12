"""Provider-independent request, result, usage and error types.

Three things are kept apart on purpose (see ADR 0005):

- the classification label (`Label`), a valid conclusion about a conversation;
- the execution status (`ExecutionStatus`), what technically happened to the call;
- security flags (`SecurityFlag`), risk signals about the input, never proof of an attack.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from whatsapp_analyzer.llm.redaction import redact


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class LLMRequest:
    """Everything a provider needs for one call. `request_id` is a hash of the other fields."""

    request_id: str
    model: str
    system: str
    messages: tuple[ChatMessage, ...]
    max_output_tokens: int
    response_schema: dict[str, object]
    prompt_id: str
    prompt_version: str
    prompt_hash: str
    context_hash: str
    temperature: float | None = None
    timeout_seconds: float = 120.0

    def __repr__(self) -> str:  # never show prompt content in tracebacks or debug output
        return f"LLMRequest(request_id={self.request_id[:12]!r}, model={self.model!r})"


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"  # output cut at max_output_tokens
    REFUSAL = "refusal"
    CONTENT_FILTER = "content_filter"
    OTHER = "other"


@dataclass(frozen=True)
class Usage:
    """Token counts in disjoint buckets. `None` means "not reported", never zero.

    Total input is the sum of the three input buckets. `output_total` is every billed output
    token, reasoning included; `output_reasoning` is the part of it spent on reasoning, for
    information only. It is never added to `output_total` again.
    """

    input_uncached: int | None = None
    input_cache_read: int | None = None
    input_cache_write: int | None = None
    output_total: int | None = None
    output_reasoning: int | None = None
    estimated: bool = False  # True for local estimates, False for provider-reported counts

    def __post_init__(self) -> None:
        for name in (
            "input_uncached",
            "input_cache_read",
            "input_cache_write",
            "output_total",
            "output_reasoning",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must not be negative")
        if (
            self.output_reasoning is not None
            and self.output_total is not None
            and self.output_reasoning > self.output_total
        ):
            raise ValueError("output_reasoning is part of output_total and cannot exceed it")

    @property
    def input_total(self) -> int | None:
        parts = (self.input_uncached, self.input_cache_read, self.input_cache_write)
        return None if any(p is None for p in parts) else sum(p for p in parts if p is not None)

    def to_dict(self) -> dict[str, object]:
        return {
            "input_uncached": self.input_uncached,
            "input_cache_read": self.input_cache_read,
            "input_cache_write": self.input_cache_write,
            "input_total": self.input_total,
            "output_total": self.output_total,
            "output_reasoning": self.output_reasoning,
            "estimated": self.estimated,
        }


@dataclass(frozen=True)
class LLMResult:
    text: str
    finish_reason: FinishReason
    usage: Usage
    model_reported: str | None = None
    provider_request_id: str | None = None

    def __repr__(self) -> str:
        return f"LLMResult(finish_reason={self.finish_reason.value!r}, chars={len(self.text)})"


class ErrorKind(StrEnum):
    AUTHENTICATION = "AUTHENTICATION"
    PERMISSION_DENIED = "PERMISSION_DENIED"  # e.g. no access to the model
    INSUFFICIENT_CREDITS = "INSUFFICIENT_CREDITS"  # billing or quota exhausted
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    OVERLOADED = "OVERLOADED"
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    CONTEXT_TOO_LONG = "CONTEXT_TOO_LONG"
    INVALID_REQUEST = "INVALID_REQUEST"
    MALFORMED_PROVIDER_RESPONSE = "MALFORMED_PROVIDER_RESPONSE"  # unparseable API body
    # An HTTP error from an endpoint whose refusals are not documented as "not processed"
    # (OpenAI-compatible servers, gateways, official SDKs behind a custom base_url). A gateway
    # can answer an error after the upstream model ran, so billing is unknown: never retried
    # automatically, only re-sent with --resend-unknown.
    ENDPOINT_ERROR = "ENDPOINT_ERROR"
    ADAPTER_ERROR = "ADAPTER_ERROR"  # unexpected exception inside an adapter (a bug)


RETRYABLE = frozenset(
    {
        ErrorKind.RATE_LIMITED,
        ErrorKind.OVERLOADED,
        ErrorKind.SERVER_ERROR,
        ErrorKind.TIMEOUT,
        ErrorKind.NETWORK,
        ErrorKind.MALFORMED_PROVIDER_RESPONSE,
    }
)
ACCOUNT_ERRORS = frozenset(
    {
        ErrorKind.AUTHENTICATION,
        ErrorKind.PERMISSION_DENIED,
        ErrorKind.INSUFFICIENT_CREDITS,
        ErrorKind.MODEL_NOT_FOUND,
    }
)


class ProviderError(Exception):
    """A failed provider call. `safe_message` is redacted and never contains request content,
    headers or credentials."""

    def __init__(
        self,
        kind: ErrorKind,
        safe_message: str = "",
        http_status: int | None = None,
        provider_code: str | None = None,
        retry_after_seconds: float | None = None,
        request_sent: bool | None = None,
    ) -> None:
        """`request_sent`: False when the adapter knows the request never left the machine
        (for example a connection that could not be opened); None when it cannot tell."""
        self.kind = kind
        self.request_sent = request_sent
        self.safe_message = redact(safe_message)
        self.http_status = http_status
        self.provider_code = redact(provider_code) if provider_code else None
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"{kind.value}: {self.safe_message}" if self.safe_message else kind.value)

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    @property
    def account_error(self) -> bool:
        return self.kind in ACCOUNT_ERRORS


class Label(StrEnum):
    """A valid conclusion. REVIEW_REQUIRED means the model found the conversation ambiguous or
    the context insufficient; it never stands for a technical failure."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class ExecutionStatus(StrEnum):
    SUCCESS = "SUCCESS"  # a validated label exists
    INVALID_RESPONSE = "INVALID_RESPONSE"  # output failed local validation
    REFUSED = "REFUSED"
    CONTENT_FILTERED = "CONTENT_FILTERED"
    INCOMPLETE_OUTPUT = "INCOMPLETE_OUTPUT"  # cut at max_output_tokens
    FAILED = "FAILED"  # provider error; see ErrorKind


class SecurityFlag(StrEnum):
    INJECTION_SUSPECTED = "INJECTION_SUSPECTED"  # heuristic risk signal, not proof
    DELIMITER_IN_CONTENT = "DELIMITER_IN_CONTENT"  # content contained (escaped) delimiters


class ReasonCode(StrEnum):
    """Observable conversation behavior only. No personal or sensitive attributes."""

    ASKS_QUESTIONS = "ASKS_QUESTIONS"
    CONFIRMS_OR_AGREES = "CONFIRMS_OR_AGREES"
    REQUESTS_FOLLOW_UP = "REQUESTS_FOLLOW_UP"
    SHORT_ACKNOWLEDGEMENTS_ONLY = "SHORT_ACKNOWLEDGEMENTS_ONLY"
    NO_REPLY = "NO_REPLY"
    DECLINES = "DECLINES"
    ASKS_TO_STOP_CONTACT = "ASKS_TO_STOP_CONTACT"
    AUTOMATED_REPLY = "AUTOMATED_REPLY"
    OFF_TOPIC = "OFF_TOPIC"
    INSUFFICIENT_CONTEXT = "INSUFFICIENT_CONTEXT"


@dataclass(frozen=True)
class Classification:
    label: Label
    reasons: tuple[ReasonCode, ...]
    evidence: tuple[str, ...]  # message ids from the prepared context, e.g. "m12"


@dataclass(frozen=True)
class ValidationFailure:
    code: str  # stable identifier, e.g. NOT_JSON, UNKNOWN_FIELD
    field: str | None = None


@dataclass
class SecurityReport:
    flags: set[SecurityFlag] = field(default_factory=set)
    suspicious_messages: list[str] = field(default_factory=list)  # message ids only
