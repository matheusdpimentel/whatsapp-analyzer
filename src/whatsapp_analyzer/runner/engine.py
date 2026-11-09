"""Execute a planned run.

Checkpoint protocol, per attempt (all writes on the event-loop thread, one writer):

1. wait for a concurrency slot and the rate limiter; check the budget with every reservation
   and reserve the attempt's worst case;
2. transaction 1: insert the attempt (`dispatched`, billing `pending`) and mark the job
   `in_flight`; commit BEFORE calling the provider;
3. call the provider (`llm.call.classify`, with its timeout);
4. transaction 2: record the outcome, billing and usage on the attempt and the job.

A crash after step 2 leaves an attempt `dispatched` with no outcome. On the next start it
becomes `abandoned` with billing `unknown`, and its job `awaiting_reconciliation`: it is never
sent again unless `--resend-unknown` is given, and its reservation stays charged.

Exactly-once delivery to a provider cannot be promised without provider-side idempotency;
the runner guarantees at-most-once automatic sending per checkpointed attempt, and that a
confirmed result is never lost or requested again.
"""

import asyncio
import hashlib
import heapq
import json
import sqlite3
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal

from whatsapp_analyzer.llm.call import CallOutcome, classify
from whatsapp_analyzer.llm.context import ContextError
from whatsapp_analyzer.llm.provider import Provider
from whatsapp_analyzer.llm.tokens import estimate_prompt_tokens
from whatsapp_analyzer.llm.types import Usage
from whatsapp_analyzer.metrics.runner import messages_of
from whatsapp_analyzer.runner.ledger import (
    Ledger,
    Reservation,
    load_ledger,
    reservation_for,
    worst_case_rate,
)
from whatsapp_analyzer.runner.limiter import RateLimiter
from whatsapp_analyzer.runner.model import (
    Billing,
    Clock,
    EventSink,
    Hooks,
    JobStatus,
    Next,
    RunStatus,
    judge,
)
from whatsapp_analyzer.runner.plan import PreparedJob, RunConfig, Selected, prepare
from whatsapp_analyzer.store.db import transaction, utc_now

RETRY_BASE_SECONDS = 1.0
RETRY_CAP_SECONDS = 60.0


@dataclass(frozen=True)
class _Job:
    id: int
    version_id: int
    conversation_id: int
    conversation_key: str
    is_group: bool
    request_id: str
    context_hash: str
    round_attempts: int
    attempts: int


@dataclass
class _Stop:
    status: RunStatus
    reason: str


def recover(connection: sqlite3.Connection) -> int:
    """Called by a writer that holds the workspace lock, so nothing else is running: runs left
    `running` crashed. Their dispatched attempts become `abandoned` with billing `unknown`, and
    the jobs await reconciliation. Returns the number of attempts recovered this way."""
    with transaction(connection):
        abandoned = connection.execute(
            "UPDATE llm_attempts SET phase = 'abandoned', billing = 'unknown', finished_at = ? "
            "WHERE phase = 'dispatched'",
            (utc_now(),),
        ).rowcount
        connection.execute(
            "UPDATE llm_jobs SET status = 'awaiting_reconciliation' WHERE status = 'in_flight'"
        )
        connection.execute("UPDATE llm_jobs SET status = 'pending' WHERE status = 'retry_wait'")
        connection.execute(
            "UPDATE llm_runs SET status = 'interrupted', stop_reason = 'CRASH' "
            "WHERE status = 'running'"
        )
    return abandoned


def requeue(
    connection: sqlite3.Connection, run_id: int, retry_failed: bool, resend_unknown: bool
) -> tuple[dict[str, int], frozenset[int]]:
    """Explicitly authorized re-sends. Earlier attempts are kept for audit, and the
    reservations of unknown attempts stay charged. Returns the counts and the ids of the jobs
    authorized by --resend-unknown."""
    counts = {"retry_failed": 0, "resend_unknown": 0}
    resent: frozenset[int] = frozenset()
    with transaction(connection):
        if resend_unknown:
            resent = frozenset(
                r[0]
                for r in connection.execute(
                    "SELECT id FROM llm_jobs "
                    "WHERE run_id = ? AND status = 'awaiting_reconciliation'",
                    (run_id,),
                )
            )
        if retry_failed:
            counts["retry_failed"] = connection.execute(
                "UPDATE llm_jobs SET status = 'pending', round_attempts = 0 "
                "WHERE run_id = ? AND status = 'failed'",
                (run_id,),
            ).rowcount
        if resend_unknown:
            counts["resend_unknown"] = connection.execute(
                "UPDATE llm_jobs SET status = 'pending', round_attempts = 0 "
                "WHERE run_id = ? AND status = 'awaiting_reconciliation'",
                (run_id,),
            ).rowcount
    return counts, resent


@dataclass
class EngineOptions:
    reuse: bool = True  # False: --no-reuse, call the provider even for known request ids
    # Jobs requeued by --resend-unknown: only these may be sent although the same request has
    # an unknown attempt elsewhere.
    resend_job_ids: frozenset[int] = frozenset()
    events: EventSink | None = None
    hooks: Hooks | None = None
    progress: Callable[[], None] | None = None


class Engine:
    def __init__(
        self,
        connection: sqlite3.Connection,
        run_id: int,
        config: RunConfig,
        provider: Provider,
        clock: Clock,
        options: EngineOptions | None = None,
    ) -> None:
        options = options or EngineOptions()
        self.connection = connection
        self.run_id = run_id
        self.config = config
        self.provider = provider
        self.clock = clock
        self.reuse = options.reuse
        self.resend_job_ids = options.resend_job_ids
        # Reuse and duplicate checks use the provider's own identity (it includes the fake
        # provider's scenario), so results of a demo scenario never feed a normal run.
        self.provider_id = provider.name
        self.events = options.events
        self.hooks = options.hooks or Hooks()
        self.progress = options.progress
        limits = config.limits
        self.ledger: Ledger = load_ledger(connection, run_id, limits.max_tokens, limits.max_cost)
        self.rate = worst_case_rate(config.price)
        self.limiter = RateLimiter(limits.requests_per_minute, limits.tokens_per_minute, clock)
        self.slots = asyncio.Semaphore(limits.concurrency)
        self.stop: _Stop | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        self.inflight_requests: set[str] = set()
        self.waiting: dict[str, list[_Job]] = {}
        self.retry_heap: list[tuple[float, int, _Job]] = []
        self.steps: dict[str, Next] = {}  # outcome of the last attempt per request id
        # request id -> job that owns a scheduled retry; its twins wait for that retry
        self.retrying: dict[str, int] = {}
        self.failure: BaseException | None = None

    # Main loop ---------------------------------------------------------------------------

    async def run(self) -> RunStatus:
        with transaction(self.connection):
            self.connection.execute(
                "UPDATE llm_runs SET status = 'running', stop_reason = NULL, finished_at = NULL "
                "WHERE id = ?",
                (self.run_id,),
            )
        self._emit("run_started", jobs=self._count_jobs())
        pending = deque(self._load(JobStatus.PENDING))
        try:
            while self.stop is None:
                job = self._next(pending)
                if job is None:
                    if self.tasks:
                        await self._wait_any()
                        continue
                    if self.retry_heap:
                        await self.clock.sleep(max(self.retry_heap[0][0] - self.clock.now(), 0))
                        continue
                    break
                await self._dispatch(job, pending)
            while self.tasks:  # let attempts already sent finish
                await self._wait_any()
        except asyncio.CancelledError:
            await self._cancel_tasks()
            self._finish(_Stop(RunStatus.INTERRUPTED, "USER_INTERRUPT"))
            raise
        except BaseException:
            # A bug, a database error or a simulated crash: stop sending at once.
            await self._cancel_tasks()
            self._finish(_Stop(RunStatus.INTERRUPTED, "INTERNAL_ERROR"))
            raise
        if self.failure is not None:
            self._abandon_leftovers()
            self._finish(_Stop(RunStatus.INTERRUPTED, "INTERNAL_ERROR"))
            raise self.failure
        status = self._finish(self.stop)
        return status

    async def _cancel_tasks(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        self._abandon_leftovers()

    def _abandon_leftovers(self) -> None:
        """Attempts of this run still `dispatched` have no recorded outcome (cancelled before
        their first step, or their result could not be recorded): their billing is unknown."""
        with transaction(self.connection):
            self.connection.execute(
                "UPDATE llm_attempts SET phase = 'abandoned', billing = 'unknown', "
                "finished_at = ? WHERE phase = 'dispatched' "
                "AND job_id IN (SELECT id FROM llm_jobs WHERE run_id = ?)",
                (utc_now(), self.run_id),
            )
            self.connection.execute(
                "UPDATE llm_jobs SET status = 'awaiting_reconciliation' "
                "WHERE run_id = ? AND status = 'in_flight'",
                (self.run_id,),
            )

    def _next(self, pending: deque[_Job]) -> _Job | None:
        if pending:
            return pending.popleft()
        if self.retry_heap and self.retry_heap[0][0] <= self.clock.now():
            return heapq.heappop(self.retry_heap)[2]
        return None

    def _coordinated(self, job: _Job) -> bool:
        """True when the job needs no call of its own: its request is in flight or waiting
        for a scheduled retry (it waits), or its outcome is already known (shared, reused or
        blocked as unknown)."""
        request_id = job.request_id
        owner = self.retrying.get(request_id)
        if request_id in self.inflight_requests or (owner is not None and owner != job.id):
            self.waiting.setdefault(request_id, []).append(job)
            return True
        if owner == job.id:
            del self.retrying[request_id]  # this job is that retry
        elif self._share_twin_outcome(job):
            return True
        if self.reuse and self._reuse(job):
            return True
        return job.id not in self.resend_job_ids and self._unknown_elsewhere(job)

    async def _dispatch(self, job: _Job, pending: deque[_Job]) -> None:
        owner = self.retrying.get(job.request_id) == job.id
        if self._coordinated(job):
            if owner:  # the retry owner ended without sending: its twins must not wait forever
                self._settle_waiting(job.request_id, pending)
            return
        prepared = self._prepare(job)
        if prepared is None:
            if owner:
                self._settle_waiting(job.request_id, pending)
            return
        estimated = estimate_prompt_tokens(
            prepared.request.system, [m.content for m in prepared.request.messages]
        )
        reservation = reservation_for(estimated, prepared.request.max_output_tokens, self.rate)
        await self.slots.acquire()
        try:
            await self.limiter.acquire(reservation.tokens)
            if self.stop is not None:
                pending.appendleft(job)
                self.slots.release()
                return
            exceeded = self.ledger.fits(reservation)
            if exceeded is not None:
                pending.appendleft(job)
                self.stop = _Stop(RunStatus.BUDGET_REACHED, f"BUDGET:{exceeded}")
                self._emit("budget_reached", limit=exceeded)
                self.slots.release()
                return
            self.ledger.reserve(reservation)
            if self.hooks.before_checkpoint:
                self.hooks.before_checkpoint(job.id)
            attempt_id, number = self._checkpoint(job, estimated, prepared, reservation)
        except BaseException:
            self.slots.release()
            raise
        if self.hooks.after_checkpoint:
            self.hooks.after_checkpoint(job.id)
        self.inflight_requests.add(job.request_id)
        task = asyncio.create_task(self._attempt(job, prepared, attempt_id, reservation, pending))
        task.add_done_callback(self._on_task_done)
        self.tasks.add(task)
        self._emit("attempt_dispatched", job_id=job.id, attempt=number)

    async def _attempt(
        self,
        job: _Job,
        prepared: PreparedJob,
        attempt_id: int,
        reservation: Reservation,
        pending: deque[_Job],
    ) -> None:
        try:
            try:
                outcome = await classify(
                    self.provider,
                    prepared.request,
                    prepared.context,
                    self.config.price,
                    clock=self.clock.now,
                )
            except asyncio.CancelledError:
                self._abandon(job, attempt_id, reservation)
                raise
            if self.hooks.after_provider_return:
                self.hooks.after_provider_return(job.id)
            self._record(job, attempt_id, reservation, outcome)
            if self.hooks.after_result_commit:
                self.hooks.after_result_commit(job.id)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            # Set before the slot is released below, so the main loop cannot dispatch again.
            if self.failure is None:
                self.failure = exc
            self.stop = self.stop or _Stop(RunStatus.INTERRUPTED, "INTERNAL_ERROR")
            raise
        finally:
            self.slots.release()
            self.inflight_requests.discard(job.request_id)
            self._settle_waiting(job.request_id, pending)

    def _on_task_done(self, task: "asyncio.Task[None]") -> None:
        """Safety net: record a failure that escaped `_attempt` (it normally sets it itself,
        synchronously, before freeing its slot)."""
        if not task.cancelled() and task.exception() is not None and self.failure is None:
            self.failure = task.exception()
            self.stop = self.stop or _Stop(RunStatus.INTERRUPTED, "INTERNAL_ERROR")

    def _settle_waiting(self, request_id: str, pending: deque[_Job]) -> None:
        """Jobs with the same request id waited for this attempt. They share its fate and are
        never sent while it is unresolved: a success is reused, a failure or an unknown
        outcome is copied (identifiably), a scheduled retry keeps them waiting."""
        waiting = self.waiting.pop(request_id, [])
        step = self.steps.pop(request_id, None)
        if not waiting:
            return
        if step is Next.RETRY:
            self.waiting[request_id] = waiting
            return
        # Otherwise they go back to the front of the queue, where _share_twin_outcome reads the
        # now recorded outcome (success, failure or unknown), or they stay pending if the run
        # is stopping.
        for w in reversed(waiting):
            pending.appendleft(w)

    async def _wait_any(self) -> None:
        """Wait for at least one attempt to finish. Failures are collected by
        `_on_task_done` and raised once the attempts already sent have finished."""
        done, _ = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
        self.tasks.difference_update(done)

    # Persistence ---------------------------------------------------------------------------

    def _load(self, status: JobStatus) -> list[_Job]:
        rows = self.connection.execute(
            "SELECT j.id, j.version_id, c.id AS conversation_id, c.conversation_key, v.is_group, "
            "j.request_id, j.context_hash, j.round_attempts, j.attempts FROM llm_jobs j "
            "JOIN conversation_versions v ON v.id = j.version_id "
            "JOIN conversations c ON c.id = v.conversation_id "
            "WHERE j.run_id = ? AND j.status = ? ORDER BY j.id",
            (self.run_id, status.value),
        ).fetchall()
        return [_Job(r[0], r[1], r[2], r[3], bool(r[4]), r[5], r[6], r[7], r[8]) for r in rows]

    def _share_twin_outcome(self, job: _Job) -> bool:
        """Identical requests are one logical request within a run, wherever their jobs sit in
        the queue. The run's own jobs are read from SQLite, so this also holds after an
        interruption: a twin that already succeeded is reused (even with --no-reuse, which
        concerns earlier runs); one that failed or awaits reconciliation is copied
        identifiably, without sending. --retry-failed and --resend-unknown requeue every twin
        together, so the requeued ones coordinate through the in-flight check instead."""
        states = {
            row[0]
            for row in self.connection.execute(
                "SELECT status FROM llm_jobs WHERE run_id = ? AND request_id = ? AND id != ? "
                "AND status IN ('succeeded', 'reused', 'failed', 'awaiting_reconciliation')",
                (self.run_id, job.request_id, job.id),
            )
        }
        if states & {JobStatus.SUCCEEDED.value, JobStatus.REUSED.value} and self._reuse(
            job, this_run_only=True
        ):
            return True
        if job.id in self.resend_job_ids:
            return False  # explicitly authorized by --resend-unknown
        if JobStatus.FAILED.value in states:
            self._copy_fate(job, JobStatus.FAILED, "DUPLICATE_OF_FAILED")
            return True
        if JobStatus.AWAITING_RECONCILIATION.value in states:
            self._copy_fate(job, JobStatus.AWAITING_RECONCILIATION, "DUPLICATE_OF_UNKNOWN")
            return True
        return False

    def _copy_fate(self, job: _Job, status: JobStatus, code: str) -> None:
        with transaction(self.connection):
            self.connection.execute(
                "UPDATE llm_jobs SET status = ?, error_kind = ? WHERE id = ?",
                (status.value, code, job.id),
            )
        self._emit("job_failed", job_id=job.id, error_kind=code)
        self._tick()

    def _reuse(self, job: _Job, this_run_only: bool = False) -> bool:
        source = self.connection.execute(
            "SELECT a.id, j.label, j.reasons_json, j.evidence_json, j.security_flags_json "
            "FROM llm_attempts a JOIN llm_jobs j ON j.id = a.job_id "
            "WHERE a.provider = ? AND a.request_id = ? AND a.execution_status = 'SUCCESS' "
            "AND a.phase = 'completed' AND (? = 0 OR j.run_id = ?) ORDER BY a.id LIMIT 1",
            (self.provider_id, job.request_id, int(this_run_only), self.run_id),
        ).fetchone()
        if source is None:
            return False
        with transaction(self.connection):
            self.connection.execute(
                "UPDATE llm_jobs SET status = 'reused', execution_status = 'SUCCESS', "
                "label = ?, reasons_json = ?, evidence_json = ?, security_flags_json = ?, "
                "reused_from_attempt = ?, error_kind = NULL, validation_error = NULL "
                "WHERE id = ?",
                (source[1], source[2], source[3], source[4], source[0], job.id),
            )
        self._emit("job_reused", job_id=job.id, from_attempt=source[0])
        self._tick()
        return True

    def _unknown_elsewhere(self, job: _Job) -> bool:
        """Another job (in any run) sent the same request with an unknown outcome: sending it
        again could be billed twice, so this job waits for reconciliation too."""
        found = self.connection.execute(
            "SELECT 1 FROM llm_attempts WHERE provider = ? AND request_id = ? AND job_id != ? "
            "AND billing IN ('unknown', 'pending') LIMIT 1",
            (self.provider_id, job.request_id, job.id),
        ).fetchone()
        if found is None:
            return False
        with transaction(self.connection):
            self.connection.execute(
                "UPDATE llm_jobs SET status = 'awaiting_reconciliation', "
                "error_kind = 'DUPLICATE_OF_UNKNOWN' WHERE id = ?",
                (job.id,),
            )
        self._emit("job_failed", job_id=job.id, error_kind="DUPLICATE_OF_UNKNOWN")
        self._tick()
        return True

    def _prepare(self, job: _Job) -> PreparedJob | None:
        """Rebuild the request. The version is immutable and the configuration is part of the
        run key, so it must match the plan; a mismatch fails the job instead of sending
        something else."""
        loaded = dict(messages_of(self.connection, [job.version_id]))
        selected = Selected(job.conversation_id, job.version_id, job.conversation_key, job.is_group)
        try:
            prepared = prepare(self.config, selected, list(loaded.get(job.version_id, [])))
        except ContextError:  # the plan can no longer be reproduced
            prepared = None
        if (
            prepared is None
            or prepared.request.request_id != job.request_id
            or prepared.context.context_hash != job.context_hash
        ):
            with transaction(self.connection):
                self.connection.execute(
                    "UPDATE llm_jobs SET status = 'failed', error_kind = 'CONTEXT_CHANGED' "
                    "WHERE id = ?",
                    (job.id,),
                )
            self._emit("job_failed", job_id=job.id, error_kind="CONTEXT_CHANGED")
            self._tick()
            return None
        return prepared

    def _checkpoint(
        self, job: _Job, estimated: int, prepared: PreparedJob, reservation: Reservation
    ) -> tuple[int, int]:
        number = job.attempts + 1
        with transaction(self.connection):
            cursor = self.connection.execute(
                "INSERT INTO llm_attempts (job_id, number, provider, request_id, phase, billing, "
                "estimated_input_tokens, max_output_tokens, reserved_tokens, reserved_cost, "
                "started_at) VALUES (?, ?, ?, ?, 'dispatched', 'pending', ?, ?, ?, ?, ?)",
                (
                    job.id,
                    number,
                    self.provider_id,
                    job.request_id,
                    estimated,
                    prepared.request.max_output_tokens,
                    reservation.tokens,
                    None if reservation.cost is None else str(reservation.cost),
                    utc_now(),
                ),
            )
            self.connection.execute(
                "UPDATE llm_jobs SET status = 'in_flight', attempts = attempts + 1, "
                "round_attempts = round_attempts + 1 WHERE id = ?",
                (job.id,),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid, number

    def _abandon(self, job: _Job, attempt_id: int, reservation: Reservation) -> None:
        """A dispatched attempt was cancelled: it may have reached the provider."""
        self.ledger.release(reservation)
        self.ledger.hold_unknown(reservation)
        with transaction(self.connection):
            self.connection.execute(
                "UPDATE llm_attempts SET phase = 'abandoned', billing = 'unknown', "
                "finished_at = ? WHERE id = ?",
                (utc_now(), attempt_id),
            )
            self.connection.execute(
                "UPDATE llm_jobs SET status = 'awaiting_reconciliation' WHERE id = ?", (job.id,)
            )
        self._emit("attempt_abandoned", job_id=job.id, billing=Billing.UNKNOWN.value)

    def _record(
        self, job: _Job, attempt_id: int, reservation: Reservation, outcome: CallOutcome
    ) -> None:
        billing, step = judge(outcome)
        telemetry = outcome.telemetry
        usage = telemetry.usage
        self.ledger.release(reservation)
        if billing is Billing.BILLED:
            self.ledger.charge(*_charged(usage, telemetry.cost.total, reservation))
        elif billing is Billing.UNKNOWN:
            self.ledger.hold_unknown(reservation)

        status, retry_at = self._next_status(job, step, telemetry.retry_after_seconds)
        # What identical waiting jobs should follow: the job's final state, so a retry that
        # ran out of attempts counts as a failure, not as a retry still to come.
        self.steps[job.request_id] = _FATE[status]
        if status is JobStatus.RETRY_WAIT:
            self.retrying[job.request_id] = job.id
        classification = outcome.classification
        with transaction(self.connection):
            self.connection.execute(
                "UPDATE llm_attempts SET phase = 'completed', billing = ?, execution_status = ?, "
                "error_kind = ?, http_status = ?, finish_reason = ?, provider_request_id = ?, "
                "input_uncached = ?, input_cache_read = ?, input_cache_write = ?, "
                "output_total = ?, output_reasoning = ?, usage_estimated = ?, cost = ?, "
                "latency_ms = ?, finished_at = ? WHERE id = ?",
                (
                    billing.value,
                    outcome.status.value,
                    telemetry.error_kind.value if telemetry.error_kind else None,
                    telemetry.http_status,
                    telemetry.finish_reason.value if telemetry.finish_reason else None,
                    telemetry.provider_request_id,
                    usage.input_uncached,
                    usage.input_cache_read,
                    usage.input_cache_write,
                    usage.output_total,
                    usage.output_reasoning,
                    int(usage.estimated),
                    None if telemetry.cost.total is None else str(telemetry.cost.total),
                    telemetry.latency_ms,
                    utc_now(),
                    attempt_id,
                ),
            )
            self.connection.execute(
                "UPDATE llm_jobs SET status = ?, execution_status = ?, error_kind = ?, "
                "validation_error = ?, label = ?, reasons_json = ?, evidence_json = ?, "
                "security_flags_json = ? WHERE id = ?",
                (
                    status.value,
                    outcome.status.value,
                    telemetry.error_kind.value if telemetry.error_kind else None,
                    telemetry.validation_error,
                    classification.label.value if classification else None,
                    json.dumps([r.value for r in classification.reasons])
                    if classification
                    else None,
                    json.dumps(list(classification.evidence)) if classification else None,
                    json.dumps(list(telemetry.security_flags)),
                    job.id,
                ),
            )
            if step is Next.STOP_RUN:  # not the job's fault: give the retry back
                self.connection.execute(
                    "UPDATE llm_jobs SET round_attempts = max(round_attempts - 1, 0) WHERE id = ?",
                    (job.id,),
                )
        if retry_at is not None:
            again = replace(job, round_attempts=job.round_attempts + 1, attempts=job.attempts + 1)
            heapq.heappush(self.retry_heap, (retry_at, job.id, again))
            self._emit(
                "retry_scheduled", job_id=job.id, delay=round(retry_at - self.clock.now(), 3)
            )
        if step is Next.STOP_RUN and self.stop is None:
            kind = telemetry.error_kind.value if telemetry.error_kind else "UNKNOWN"
            self.stop = _Stop(RunStatus.PAUSED, f"ACCOUNT_ERROR:{kind}")
            self._emit("run_paused", reason=self.stop.reason)
        self._emit(
            "attempt_finished",
            job_id=job.id,
            status=outcome.status.value,
            billing=billing.value,
            error_kind=telemetry.error_kind.value if telemetry.error_kind else None,
            job_status=status.value,
        )
        self._tick()

    def _next_status(
        self, job: _Job, step: Next, retry_after: float | None
    ) -> tuple[JobStatus, float | None]:
        if step is Next.SUCCEEDED:
            return JobStatus.SUCCEEDED, None
        if step is Next.RECONCILE:
            return JobStatus.AWAITING_RECONCILIATION, None
        if step is Next.STOP_RUN:
            return JobStatus.PENDING, None  # not the job's fault; resumes later
        if step is Next.RETRY and job.round_attempts + 1 < self.config.limits.max_attempts:
            delay = _backoff(job.request_id, job.round_attempts + 1, retry_after)
            return JobStatus.RETRY_WAIT, self.clock.now() + delay
        return JobStatus.FAILED, None

    def _finish(self, stop: _Stop | None) -> RunStatus:
        if stop is None and self._count_jobs().get("pending", 0):
            # Never report a finished run while jobs are still pending: keep it resumable.
            stop = _Stop(RunStatus.INTERRUPTED, "PENDING_JOBS_LEFT")
        if stop is None:
            open_jobs = self.connection.execute(
                "SELECT count(*) FROM llm_jobs WHERE run_id = ? "
                "AND status IN ('failed', 'awaiting_reconciliation')",
                (self.run_id,),
            ).fetchone()[0]
            stop = _Stop(
                RunStatus.COMPLETED_WITH_FAILURES if open_jobs else RunStatus.COMPLETED, ""
            )
        with transaction(self.connection):
            self.connection.execute(
                "UPDATE llm_jobs SET status = 'pending' WHERE run_id = ? AND status = 'retry_wait'",
                (self.run_id,),
            )
            self.connection.execute(
                "UPDATE llm_runs SET status = ?, stop_reason = ?, finished_at = ? WHERE id = ?",
                (stop.status.value, stop.reason or None, utc_now(), self.run_id),
            )
        self._emit("run_finished", status=stop.status.value, reason=stop.reason or None)
        return stop.status

    def _count_jobs(self) -> dict[str, int]:
        return dict(
            self.connection.execute(
                "SELECT status, count(*) FROM llm_jobs WHERE run_id = ? GROUP BY status",
                (self.run_id,),
            ).fetchall()
        )

    def _emit(self, event: str, **fields: object) -> None:
        if self.events is not None:
            self.events({"event": event, "run_id": self.run_id, **fields})

    def _tick(self) -> None:
        if self.progress is not None:
            self.progress()


_FATE = {
    JobStatus.SUCCEEDED: Next.SUCCEEDED,
    JobStatus.FAILED: Next.FAILED,
    JobStatus.RETRY_WAIT: Next.RETRY,
    JobStatus.AWAITING_RECONCILIATION: Next.RECONCILE,
    JobStatus.PENDING: Next.STOP_RUN,
}


def _charged(
    usage: Usage, cost: Decimal | None, reservation: Reservation
) -> tuple[int, Decimal | None]:
    """Actual tokens and cost when all are known; otherwise the reservation (never zero). The
    cost stays None when neither is known."""
    tokens = (
        usage.input_total + usage.output_total
        if usage.input_total is not None and usage.output_total is not None
        else reservation.tokens
    )
    return tokens, cost if cost is not None else reservation.cost


def _backoff(request_id: str, attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with deterministic jitter (up to 25%), never below `retry_after`."""
    base = min(RETRY_BASE_SECONDS * float(2 ** (attempt - 1)), RETRY_CAP_SECONDS)
    digest = hashlib.sha256(f"{request_id}:{attempt}".encode()).digest()
    jitter = base * 0.25 * digest[0] / 255
    return max(base + jitter, retry_after or 0.0)
