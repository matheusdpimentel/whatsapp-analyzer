"""Entry points used by the CLI and tests: open (plan, resume or reuse) a run, then execute it.

The caller must hold the workspace writer lock for both steps.

- `--force`: plan a NEW run even when a run with the same key and scope exists. Results of
  identical requests are still reused unless `--no-reuse` is also given.
- `--no-reuse`: inside the run being executed, call the provider even when a successful
  result for the same provider and request id exists. It does not create a new run by itself.
- `--retry-failed`: send failed jobs again (their earlier attempts were billed, rejected or
  never sent). It never includes jobs awaiting reconciliation.
- `--resend-unknown`: send jobs awaiting reconciliation again. This may be billed twice.
"""

import json
import sqlite3
from dataclasses import dataclass, replace
from decimal import Decimal

from whatsapp_analyzer.config import LimitSettings
from whatsapp_analyzer.llm.context import ContextError
from whatsapp_analyzer.llm.pricing import Price
from whatsapp_analyzer.llm.provider import Provider
from whatsapp_analyzer.llm.tokens import estimate_prompt_tokens
from whatsapp_analyzer.metrics.runner import messages_of
from whatsapp_analyzer.runner.engine import Engine, EngineOptions, recover, requeue
from whatsapp_analyzer.runner.ledger import worst_case_rate
from whatsapp_analyzer.runner.model import RESUMABLE, Clock, RunStatus
from whatsapp_analyzer.runner.plan import (
    NothingSelectedError,
    RunConfig,
    Selected,
    create_run,
    find_run,
    limits_json,
    load_run,
    mark_used,
    prepare,
    price_from_json,
    price_json,
    scope_fingerprint,
    select_versions,
)
from whatsapp_analyzer.store.db import transaction


class ConflictingOptionsError(Exception):
    """`--run` selects an existing run, so `--conversation` and `--force` cannot apply."""


class BudgetUnverifiableError(Exception):
    """A hard cost limit was requested but the price is missing or incomplete."""


@dataclass(frozen=True)
class OpenRequest:
    keys: tuple[str, ...] | None = None
    run_id: int | None = None
    force: bool = False
    retry_failed: bool = False
    resend_unknown: bool = False
    # Limits given explicitly on the command line. A resumed run keeps the limits it was
    # started with and changes only these, so a budget is never dropped by omission.
    limit_overrides: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True)
class OpenedRun:
    run_id: int
    mode: str  # new | resumed | unchanged | up_to_date
    config: RunConfig  # with the run's own price snapshot
    recovered_attempts: int
    requeued: dict[str, int]
    resend_job_ids: frozenset[int]  # authorized by --resend-unknown


def open_run(
    connection: sqlite3.Connection,
    config: RunConfig,
    request: OpenRequest,
    *,
    defer_requeue: bool = False,
) -> OpenedRun:
    if request.run_id is not None and (request.keys or request.force):
        raise ConflictingOptionsError
    recovered = recover(connection)
    row: sqlite3.Row | None
    if request.run_id is not None:
        row = load_run(connection, request.run_id, config)
    else:
        selection = select_versions(connection, request.keys)
        if not selection:
            raise NothingSelectedError
        row = None if request.force else find_run(connection, config, scope_fingerprint(selection))
        if row is None:
            _check_budget(config)
            run_id = create_run(connection, config, selection)
            return OpenedRun(run_id, "new", config, recovered, {}, frozenset())

    price = with_known_tier(price_from_json(row["price_json"]), config.price)
    run_config = replace(
        config,
        price=price,
        limits=stored_limits(row["limits_json"], dict(request.limit_overrides)),
    )
    _check_budget(run_config)
    with transaction(connection):
        connection.execute(
            "UPDATE llm_runs SET limits_json = ?, price_json = ? WHERE id = ?",
            (limits_json(run_config.limits), price_json(price), row["id"]),
        )
        mark_used(connection, row["id"])
    opened = OpenedRun(row["id"], "", run_config, recovered, {}, frozenset())
    if defer_requeue:  # the caller authorizes first, then calls apply_requeue
        return replace(opened, mode=_mode(connection, row["id"]))
    return apply_requeue(connection, opened, request.retry_failed, request.resend_unknown)


def apply_requeue(
    connection: sqlite3.Connection, opened: OpenedRun, retry_failed: bool, resend_unknown: bool
) -> OpenedRun:
    """Requeue explicitly re-sent jobs (`--retry-failed`, `--resend-unknown`) and set the mode.
    With a real provider this runs only after the invocation was authorized, so a refused
    authorization never leaves a re-send pending for a later invocation."""
    requeued, resent = requeue(connection, opened.run_id, retry_failed, resend_unknown)
    return replace(
        opened, mode=_mode(connection, opened.run_id), requeued=requeued, resend_job_ids=resent
    )


def requeue_statuses(retry_failed: bool, resend_unknown: bool) -> tuple[str, ...]:
    """Job statuses that `apply_requeue` would make pending (for the estimate)."""
    statuses = ["pending"]
    if retry_failed:
        statuses.append("failed")
    if resend_unknown:
        statuses.append("awaiting_reconciliation")
    return tuple(statuses)


def _mode(connection: sqlite3.Connection, run_id: int) -> str:
    pending = connection.execute(
        "SELECT count(*) FROM llm_jobs WHERE run_id = ? AND status = 'pending'", (run_id,)
    ).fetchone()[0]
    status = RunStatus(
        connection.execute("SELECT status FROM llm_runs WHERE id = ?", (run_id,)).fetchone()[0]
    )
    if pending or status in RESUMABLE:
        return "resumed"
    if status is RunStatus.COMPLETED_WITH_FAILURES:
        return "unchanged"  # open jobs need --retry-failed or --resend-unknown
    return "up_to_date"


def with_known_tier(snapshot: Price | None, current: Price | None) -> Price | None:
    """A run keeps the prices it started with, but a snapshot taken without tier information
    gains the tier ceiling and long rates of the current entry for the same model, so a
    resumed run never keeps costing long requests at the cheaper short-context rates."""
    if (
        snapshot is None
        or current is None
        or snapshot.max_input_tokens is not None
        or current.max_input_tokens is None
        or (snapshot.provider, snapshot.model) != (current.provider, current.model)
    ):
        return snapshot

    def at_least(long_rate: Decimal | None, short_rate: Decimal | None) -> Decimal | None:
        # a long rate is never cheaper than the snapshot's own short rate
        if long_rate is None or short_rate is None:
            return long_rate
        return max(long_rate, short_rate)

    return replace(
        snapshot,
        max_input_tokens=current.max_input_tokens,
        long_input=at_least(current.long_input, snapshot.input),
        long_cache_read=at_least(current.long_cache_read, snapshot.cache_read),
        long_cache_write=at_least(current.long_cache_write, snapshot.cache_write),
        long_output=at_least(current.long_output, snapshot.output),
    )


@dataclass(frozen=True)
class SendEstimate:
    """Upper bounds of what executing a run may send, shown before authorizing it.

    Identical requests are counted once; reuse can only lower the real numbers, and the
    run's limits still apply on top of them. Token figures are local estimates."""

    jobs: int  # jobs that may be sent: pending ones and those an explicit re-send requeues
    requests: int  # distinct requests among them
    max_requests: int  # requests x max attempts
    estimated_input_tokens: int  # all distinct requests, once each
    max_output_tokens: int  # per request
    worst_case_tokens: int  # every attempt at its estimated input plus maximum output
    worst_case_cost: Decimal | None  # None: no complete price
    currency: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "jobs": self.jobs,
            "requests": self.requests,
            "max_requests": self.max_requests,
            "estimated_input_tokens": self.estimated_input_tokens,
            "max_output_tokens_per_request": self.max_output_tokens,
            "worst_case_tokens": self.worst_case_tokens,
            "worst_case_cost": None if self.worst_case_cost is None else str(self.worst_case_cost),
            "currency": self.currency,
        }


def send_estimate(
    connection: sqlite3.Connection,
    opened: OpenedRun,
    statuses: tuple[str, ...] = ("pending",),
) -> SendEstimate:
    """Read-only: prepares the jobs in `statuses` again to estimate them (pending jobs, plus
    the jobs an explicit re-send would requeue)."""
    marks = ",".join("?" * len(statuses))
    rows = connection.execute(
        "SELECT j.version_id, c.id, c.conversation_key, v.is_group, j.request_id "
        "FROM llm_jobs j JOIN conversation_versions v ON v.id = j.version_id "
        "JOIN conversations c ON c.id = v.conversation_id "
        f"WHERE j.run_id = ? AND j.status IN ({marks}) ORDER BY j.id",
        (opened.run_id, *statuses),
    ).fetchall()
    config = opened.config
    distinct: dict[str, Selected] = {}
    for row in rows:
        distinct.setdefault(row[4], Selected(row[1], row[0], row[2], bool(row[3])))
    loaded = dict(messages_of(connection, [s.version_id for s in distinct.values()]))
    estimated = 0
    for selected in distinct.values():
        try:
            request = prepare(config, selected, list(loaded.get(selected.version_id, []))).request
        except ContextError:  # it fails without sending when the run executes
            continue
        estimated += estimate_prompt_tokens(request.system, [m.content for m in request.messages])
    attempts = config.limits.max_attempts
    max_output = config.max_output_tokens
    worst_tokens = (estimated + len(distinct) * max_output) * attempts
    rate = worst_case_rate(config.price)
    return SendEstimate(
        jobs=len(rows),
        requests=len(distinct),
        max_requests=len(distinct) * attempts,
        estimated_input_tokens=estimated,
        max_output_tokens=max_output,
        worst_case_tokens=worst_tokens,
        worst_case_cost=None if rate is None else rate * worst_tokens,
        currency=config.price.currency if config.price is not None else None,
    )


def stored_limits(text: str, overrides: dict[str, object]) -> LimitSettings:
    values = json.loads(text)
    if values.get("max_cost") is not None:
        values["max_cost"] = Decimal(values["max_cost"])
    values.update(overrides)
    return LimitSettings(**values)


def _check_budget(config: RunConfig) -> None:
    if config.limits.max_cost is not None and worst_case_rate(config.price) is None:
        raise BudgetUnverifiableError


async def execute(
    connection: sqlite3.Connection,
    opened: OpenedRun,
    provider: Provider,
    clock: Clock,
    options: EngineOptions | None = None,
) -> RunStatus:
    engine = Engine(connection, opened.run_id, opened.config, provider, clock, options)
    return await engine.run()
