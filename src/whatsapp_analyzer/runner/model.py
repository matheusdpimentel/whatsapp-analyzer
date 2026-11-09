"""States, outcome policy and the clock used by the runner."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from whatsapp_analyzer.llm.call import CallOutcome
from whatsapp_analyzer.llm.types import ErrorKind, ExecutionStatus


class RunStatus(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"  # stopped by an account error; resumable
    INTERRUPTED = "interrupted"  # Ctrl+C or a crash; resumable
    BUDGET_REACHED = "budget_reached"  # resumable with a higher budget
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"  # failed or unreconciled jobs remain


RESUMABLE = frozenset(
    {RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.INTERRUPTED, RunStatus.BUDGET_REACHED}
)


class JobStatus(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    RETRY_WAIT = "retry_wait"
    AWAITING_RECONCILIATION = "awaiting_reconciliation"  # outcome or billing unknown
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REUSED = "reused"
    SKIPPED = "skipped"


class Billing(StrEnum):
    """What an attempt may have cost. Only `not_sent` and `rejected` are safe to send again."""

    PENDING = "pending"  # checkpointed, outcome not recorded yet
    NOT_SENT = "not_sent"  # the request never left the machine
    REJECTED = "rejected"  # the provider refused it before processing
    BILLED = "billed"  # a response came back; usage applies
    UNKNOWN = "unknown"  # it may have been processed and billed


class Next(StrEnum):
    """What happens to a job after an attempt."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRY = "retry"  # safe automatic retry (rejected or not sent)
    RECONCILE = "reconcile"  # outcome unknown: never retried without --resend-unknown
    STOP_RUN = "stop_run"  # account error: pause the run, job stays pending


# The provider explicitly refused these before processing the request.
_REJECTED = frozenset(
    {
        ErrorKind.AUTHENTICATION,
        ErrorKind.PERMISSION_DENIED,
        ErrorKind.INSUFFICIENT_CREDITS,
        ErrorKind.MODEL_NOT_FOUND,
        ErrorKind.RATE_LIMITED,
        ErrorKind.OVERLOADED,
        ErrorKind.CONTEXT_TOO_LONG,
        ErrorKind.INVALID_REQUEST,
    }
)
# Rejected, and worth retrying after a pause.
_SAFE_TO_RETRY = frozenset({ErrorKind.RATE_LIMITED, ErrorKind.OVERLOADED})
_ACCOUNT = frozenset(
    {
        ErrorKind.AUTHENTICATION,
        ErrorKind.PERMISSION_DENIED,
        ErrorKind.INSUFFICIENT_CREDITS,
        ErrorKind.MODEL_NOT_FOUND,
    }
)


def judge(outcome: CallOutcome) -> tuple[Billing, Next]:
    """Billing and next step for one attempt.

    TIMEOUT, NETWORK, SERVER_ERROR, MALFORMED_PROVIDER_RESPONSE and ADAPTER_ERROR may happen
    after the provider processed the request, so they are `unknown` and need reconciliation,
    unless the adapter states that the request was never sent.
    """
    telemetry = outcome.telemetry
    if outcome.status is not ExecutionStatus.FAILED:
        if outcome.status is ExecutionStatus.SUCCESS:
            return Billing.BILLED, Next.SUCCEEDED
        return Billing.BILLED, Next.FAILED  # invalid, refused, filtered, truncated
    kind = telemetry.error_kind
    assert kind is not None
    if kind in _ACCOUNT:
        return Billing.REJECTED, Next.STOP_RUN
    if kind in _REJECTED:
        return Billing.REJECTED, Next.RETRY if kind in _SAFE_TO_RETRY else Next.FAILED
    if telemetry.request_sent is False:
        return Billing.NOT_SENT, Next.RETRY if telemetry.retryable else Next.FAILED
    return Billing.UNKNOWN, Next.RECONCILE


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """A clock for tests: sleeping advances time instantly."""

    def __init__(self, start: float = 0.0) -> None:
        self.time = start
        self.slept: list[float] = []

    def now(self) -> float:
        return self.time

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.time += max(seconds, 0.0)
        await asyncio.sleep(0)


Hook = Callable[[int], None]  # receives the job id


@dataclass
class Hooks:
    """Test seams around the checkpoint protocol. Production code leaves them empty."""

    before_checkpoint: Hook | None = None
    after_checkpoint: Hook | None = None
    after_provider_return: Hook | None = None
    after_result_commit: Hook | None = None


Event = dict[str, object]
EventSink = Callable[[Event], None]


@dataclass
class RunSummary:
    run_id: int
    mode: str  # new | resumed | up_to_date
    status: RunStatus
    stop_reason: str | None
    jobs: dict[str, int] = field(default_factory=dict)
    totals: dict[str, object] = field(default_factory=dict)
