"""Per-call telemetry. It holds identifiers, hashes, counts and codes only: never prompts,
message text, raw responses or credentials. Milestone 4 keeps it in memory; milestone 5
persists it."""

from dataclasses import dataclass

from whatsapp_analyzer.llm.pricing import CostEstimate
from whatsapp_analyzer.llm.types import ErrorKind, ExecutionStatus, FinishReason, Label, Usage


@dataclass(frozen=True)
class CallTelemetry:
    request_id: str
    provider: str
    model: str
    model_reported: str | None
    provider_request_id: str | None
    prompt_id: str
    prompt_version: str
    prompt_hash: str
    context_hash: str
    status: ExecutionStatus
    label: Label | None
    finish_reason: FinishReason | None
    error_kind: ErrorKind | None
    http_status: int | None
    retryable: bool | None
    validation_error: str | None
    security_flags: tuple[str, ...]
    usage: Usage
    cost: CostEstimate
    latency_ms: int
    request_sent: bool | None = None  # from ProviderError: False = never left the machine
    retry_after_seconds: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "provider": self.provider,
            "model": self.model,
            "model_reported": self.model_reported,
            "provider_request_id": self.provider_request_id,
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "prompt_hash": self.prompt_hash,
            "context_hash": self.context_hash,
            "status": self.status.value,
            "label": self.label.value if self.label else None,
            "finish_reason": self.finish_reason.value if self.finish_reason else None,
            "error_kind": self.error_kind.value if self.error_kind else None,
            "http_status": self.http_status,
            "retryable": self.retryable,
            "validation_error": self.validation_error,
            "security_flags": list(self.security_flags),
            "usage": self.usage.to_dict(),
            "cost": self.cost.to_dict(),
            "latency_ms": self.latency_ms,
            "request_sent": self.request_sent,
            "retry_after_seconds": self.retry_after_seconds,
        }
