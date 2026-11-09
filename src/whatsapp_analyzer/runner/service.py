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
from whatsapp_analyzer.llm.provider import Provider
from whatsapp_analyzer.runner.engine import Engine, EngineOptions, recover, requeue
from whatsapp_analyzer.runner.ledger import worst_case_rate
from whatsapp_analyzer.runner.model import RESUMABLE, Clock, RunStatus
from whatsapp_analyzer.runner.plan import (
    NothingSelectedError,
    RunConfig,
    create_run,
    find_run,
    limits_json,
    load_run,
    mark_used,
    price_from_json,
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


def open_run(connection: sqlite3.Connection, config: RunConfig, request: OpenRequest) -> OpenedRun:
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

    run_config = replace(
        config,
        price=price_from_json(row["price_json"]),
        limits=stored_limits(row["limits_json"], dict(request.limit_overrides)),
    )
    _check_budget(run_config)
    requeued, resent = requeue(connection, row["id"], request.retry_failed, request.resend_unknown)
    with transaction(connection):
        connection.execute(
            "UPDATE llm_runs SET limits_json = ? WHERE id = ?",
            (limits_json(run_config.limits), row["id"]),
        )
        mark_used(connection, row["id"])
    pending = connection.execute(
        "SELECT count(*) FROM llm_jobs WHERE run_id = ? AND status = 'pending'", (row["id"],)
    ).fetchone()[0]
    status = RunStatus(row["status"])
    if pending or status in RESUMABLE:
        mode = "resumed"
    elif status is RunStatus.COMPLETED_WITH_FAILURES:
        mode = "unchanged"  # open jobs need --retry-failed or --resend-unknown
    else:
        mode = "up_to_date"
    return OpenedRun(row["id"], mode, run_config, recovered, requeued, resent)


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
