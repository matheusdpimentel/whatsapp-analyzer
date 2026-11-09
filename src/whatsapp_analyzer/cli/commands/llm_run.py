"""`llm run`, `llm status` and `llm export`: the persistent runner (offline FakeProvider only).

Exit codes: 0 completed; 8 completed with failed or unreconciled jobs; 7 budget reached;
5 paused by an account error; 6 interrupted (resumable). With `--events FILE` (or `-` for
stderr) one JSON object per event is written, with identifiers and codes only; stdout still
carries exactly one JSON document with `--json`.
"""

import asyncio
import json
import sqlite3
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, TextIO

import typer
from rich.progress import Progress, TaskID
from rich.table import Table

from whatsapp_analyzer.cli.commands.llm import PRICING_FILE, PROMPTS_DIR, prompt_error
from whatsapp_analyzer.cli.output import (
    CliState,
    result_payload,
    stderr_console,
    stdout_console,
    write_json,
)
from whatsapp_analyzer.cli.workspace_support import (
    WorkspaceOption,
    config_error,
    load_settings,
    open_database,
    resolve_workspace,
    writer_lock,
)
from whatsapp_analyzer.config import ConfigError, LimitSettings, Settings
from whatsapp_analyzer.errors import AppError
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.i18n import t
from whatsapp_analyzer.llm.fake import FakeProvider, FakeScenario
from whatsapp_analyzer.llm.pricing import load_prices
from whatsapp_analyzer.llm.prompts import PromptError, find_template
from whatsapp_analyzer.llm.types import ErrorKind
from whatsapp_analyzer.runner.engine import EngineOptions
from whatsapp_analyzer.runner.model import Event, RunStatus, SystemClock
from whatsapp_analyzer.runner.plan import NothingSelectedError, RunConfig, RunConfigChangedError
from whatsapp_analyzer.runner.reports import LlmRunNotFoundError, export_llm_run, run_report
from whatsapp_analyzer.runner.service import (
    BudgetUnverifiableError,
    ConflictingOptionsError,
    OpenedRun,
    OpenRequest,
    execute,
    open_run,
)
from whatsapp_analyzer.store.lock import WorkspaceBusyError
from whatsapp_analyzer.workspace import Workspace

EXIT_FOR_STATUS = {
    RunStatus.COMPLETED: ExitCode.OK,
    RunStatus.COMPLETED_WITH_FAILURES: ExitCode.COMPLETED_WITH_FAILURES,
    RunStatus.BUDGET_REACHED: ExitCode.BUDGET_REACHED,
    RunStatus.PAUSED: ExitCode.PROVIDER_ACCOUNT_ERROR,
    RunStatus.INTERRUPTED: ExitCode.RUN_PAUSED,
    RunStatus.RUNNING: ExitCode.RUN_PAUSED,
}


def run(
    ctx: typer.Context,
    workspace: WorkspaceOption = None,
    config: Annotated[
        Path | None, typer.Option(metavar="FILE", help=t("llm.run.option.config"))
    ] = None,
    prompt: Annotated[
        str | None, typer.Option(metavar="ID|FILE", help=t("llm.preview.option.prompt"))
    ] = None,
    conversation: Annotated[
        list[str] | None,
        typer.Option(metavar="KEY", help=t("llm.run.option.conversation")),
    ] = None,
    run_id: Annotated[
        int | None, typer.Option("--run", metavar="ID", help=t("llm.run.option.run"))
    ] = None,
    force: Annotated[bool, typer.Option(help=t("llm.run.option.force"))] = False,
    reuse: Annotated[bool, typer.Option(help=t("llm.run.option.reuse"))] = True,
    retry_failed: Annotated[bool, typer.Option(help=t("llm.run.option.retry_failed"))] = False,
    resend_unknown: Annotated[bool, typer.Option(help=t("llm.run.option.resend_unknown"))] = False,
    concurrency: Annotated[
        int | None, typer.Option(min=1, max=64, help=t("llm.run.option.concurrency"))
    ] = None,
    rpm: Annotated[int | None, typer.Option(min=1, help=t("llm.run.option.rpm"))] = None,
    tpm: Annotated[int | None, typer.Option(min=1, help=t("llm.run.option.tpm"))] = None,
    max_tokens: Annotated[
        int | None, typer.Option(min=1, help=t("llm.run.option.max_tokens"))
    ] = None,
    max_cost: Annotated[
        str | None, typer.Option(metavar="AMOUNT", help=t("llm.run.option.max_cost"))
    ] = None,
    max_attempts: Annotated[
        int | None, typer.Option(min=1, max=10, help=t("llm.run.option.max_attempts"))
    ] = None,
    fake_scenario: Annotated[
        str | None, typer.Option(metavar="SCENARIO", help=t("llm.run.option.fake_scenario"))
    ] = None,
    events: Annotated[
        str | None, typer.Option(metavar="FILE|-", help=t("llm.run.option.events"))
    ] = None,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    settings = load_settings(target, config)
    overrides = _overrides(concurrency, rpm, tpm, max_tokens, max_cost, max_attempts)
    limits = replace(settings.llm.limits, **overrides)
    provider = _provider(fake_scenario)
    run_config = replace(_run_config(target, settings, prompt, limits), provider=provider.name)
    request = OpenRequest(
        keys=tuple(conversation) if conversation else None,
        run_id=run_id,
        force=force,
        retry_failed=retry_failed,
        resend_unknown=resend_unknown,
        limit_overrides=tuple(overrides.items()),
    )
    with ExitStack() as stack:
        stack.enter_context(writer_lock(target))
        connection = open_database(target)
        stack.callback(connection.close)
        opened = _open(connection, run_config, request)
        sink = _event_sink(stack, events)
        quiet = events == "-"  # events on stderr: no progress bar to interleave with
        status = RunStatus(
            connection.execute(
                "SELECT status FROM llm_runs WHERE id = ?", (opened.run_id,)
            ).fetchone()[0]
        )
        if opened.mode in {"new", "resumed"}:
            progress = None if quiet else _progress(stack, state, connection, opened.run_id)
            options = EngineOptions(
                reuse=reuse,
                resend_job_ids=opened.resend_job_ids,
                events=sink,
                progress=progress,
            )
            try:
                status = asyncio.run(execute(connection, opened, provider, SystemClock(), options))
            except KeyboardInterrupt:
                status = RunStatus.INTERRUPTED
        report = run_report(connection, opened.run_id)

    data: dict[str, object] = {
        "run_id": opened.run_id,
        "mode": opened.mode,
        "status": status.value,
        "recovered_attempts": opened.recovered_attempts,
        "requeued": opened.requeued,
        **report,
    }
    failure = _failure(status, report)
    if state.json_output:
        write_json(result_payload("llm run", data, failure))
    else:
        _render(data)
        if failure is not None:
            stderr_console().print(failure.message, style="yellow", highlight=False)
    raise typer.Exit(EXIT_FOR_STATUS[status])


def status(
    ctx: typer.Context,
    workspace: WorkspaceOption = None,
    run_id: Annotated[
        int | None, typer.Option("--run", metavar="ID", help=t("llm.status.option.run"))
    ] = None,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    connection = open_database(target)
    try:
        report = run_report(connection, run_id)
    except LlmRunNotFoundError as exc:
        raise _not_found(exc) from exc
    finally:
        connection.close()
    run_info = report["run"]
    assert isinstance(run_info, dict)
    # A run shown as running while no writer holds the lock crashed; the next writer will
    # recover it. Status itself never writes.
    # The probe takes the writer lock for an instant (a writer starting at that moment gets
    # WORKSPACE_BUSY and can simply be run again); the status is read again afterwards.
    report["stale"] = run_info["status"] == RunStatus.RUNNING and _crashed(target, run_info["id"])
    if state.json_output:
        write_json(result_payload("llm status", report))
        return
    _render(report)


def export(
    ctx: typer.Context,
    out: Annotated[Path, typer.Option(metavar="FILE", help=t("export.option.out"))],
    workspace: WorkspaceOption = None,
    run_id: Annotated[
        int | None, typer.Option("--run", metavar="ID", help=t("llm.status.option.run"))
    ] = None,
    overwrite: Annotated[bool, typer.Option(help=t("export.option.overwrite"))] = False,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    metadata = out.with_name(out.name + ".meta.json")
    for path in (out, metadata):
        if path.exists() and not overwrite:
            raise AppError(
                "OUTPUT_EXISTS",
                ExitCode.USAGE_ERROR,
                t("export.error.output_exists", path=path),
                hint=t("export.hint.overwrite"),
            )
    if out.is_dir() or not out.parent.is_dir():
        raise AppError(
            "OUTPUT_NOT_WRITABLE", ExitCode.OUTPUT_ERROR, t("export.error.invalid_path", path=out)
        )
    connection = open_database(target)
    try:
        result = export_llm_run(connection, run_id, out)
    except LlmRunNotFoundError as exc:
        raise _not_found(exc) from exc
    except OSError as exc:
        raise AppError(
            "OUTPUT_NOT_WRITABLE",
            ExitCode.OUTPUT_ERROR,
            t("export.error.not_writable", path=out, reason=exc.strerror or type(exc).__name__),
        ) from exc
    finally:
        connection.close()
    run_info = result.metadata["run"]
    assert isinstance(run_info, dict)
    data = {
        "path": str(result.path),
        "metadata_path": str(result.metadata_path),
        "rows": result.rows,
        "run_id": run_info["id"],
        "run_status": run_info["status"],
        "partial": run_info["partial"],
        "awaiting_reconciliation_rows": result.metadata["awaiting_reconciliation_rows"],
        "superseded_rows": result.metadata["superseded_rows"],
    }
    if state.json_output:
        write_json(result_payload("llm export", data))
        return
    stdout_console().print(
        t("llm.export.written", rows=result.rows, run_id=run_info["id"], path=result.path),
        markup=False,
        highlight=False,
    )
    if run_info["partial"]:
        stderr_console().print(t("llm.export.partial", status=run_info["status"]), style="yellow")
    if data["awaiting_reconciliation_rows"]:
        stderr_console().print(
            t("llm.export.reconcile", count=data["awaiting_reconciliation_rows"]), style="yellow"
        )


# Helpers -------------------------------------------------------------------------------


def _overrides(
    concurrency: int | None,
    rpm: int | None,
    tpm: int | None,
    max_tokens: int | None,
    max_cost: str | None,
    max_attempts: int | None,
) -> dict[str, Any]:
    """Only the limits given explicitly on the command line."""
    overrides: dict[str, Any] = {}
    for name, value in (
        ("concurrency", concurrency),
        ("requests_per_minute", rpm),
        ("tokens_per_minute", tpm),
        ("max_tokens", max_tokens),
        ("max_attempts", max_attempts),
    ):
        if value is not None:
            overrides[name] = value
    if max_cost is not None:
        try:
            cost = Decimal(max_cost)
        except InvalidOperation as exc:
            raise AppError(
                "INVALID_OPTION", ExitCode.USAGE_ERROR, t("llm.run.error.max_cost", value=max_cost)
            ) from exc
        if not cost.is_finite() or cost < 0:
            raise AppError(
                "INVALID_OPTION", ExitCode.USAGE_ERROR, t("llm.run.error.max_cost", value=max_cost)
            )
        overrides["max_cost"] = cost
    return overrides


def _run_config(
    workspace: Workspace, settings: Settings, prompt: str | None, limits: LimitSettings
) -> RunConfig:
    try:
        template = find_template(
            prompt or settings.llm.prompt,
            workspace.root / PROMPTS_DIR,
            base_dir=None if prompt else workspace.root,
        )
    except PromptError as exc:
        raise prompt_error(exc) from exc
    try:
        prices = load_prices(workspace.root / PRICING_FILE)
    except ConfigError as exc:
        raise config_error(exc) from exc
    return RunConfig(
        provider=settings.llm.provider,
        model=settings.llm.model,
        template=template,
        context=settings.llm.context,
        ignored_texts=settings.metrics.ignored_texts,
        limits=limits,
        price=prices.lookup(settings.llm.provider, settings.llm.model, date.today()),
    )


def _provider(scenario: str | None) -> FakeProvider:
    if scenario is None:
        return FakeProvider()
    name, _, kind = scenario.partition(":")
    try:
        chosen = FakeScenario(name)
        error_kind = ErrorKind(kind) if kind else ErrorKind.SERVER_ERROR
    except ValueError as exc:
        choices = ", ".join(s.value for s in FakeScenario)
        raise AppError(
            "INVALID_OPTION",
            ExitCode.USAGE_ERROR,
            t("llm.run.error.fake_scenario", value=scenario, choices=choices),
        ) from exc
    return FakeProvider(chosen, error_kind=error_kind)


def _open(connection: sqlite3.Connection, config: RunConfig, request: OpenRequest) -> OpenedRun:
    try:
        return open_run(connection, config, request)
    except NothingSelectedError as exc:
        raise AppError(
            "NO_CONVERSATIONS",
            ExitCode.USAGE_ERROR,
            t("llm.run.error.no_conversations"),
            hint=t("metrics.hint.no_conversations"),
        ) from exc
    except BudgetUnverifiableError as exc:
        raise AppError(
            "BUDGET_UNVERIFIABLE",
            ExitCode.CONFIG_ERROR,
            t("llm.run.error.budget_unverifiable"),
            hint=t("llm.run.hint.budget_unverifiable"),
        ) from exc
    except RunConfigChangedError as exc:
        raise AppError(
            "RUN_CONFIG_CHANGED",
            ExitCode.CONFIG_ERROR,
            t("llm.run.error.config_changed"),
            hint=t("llm.run.hint.config_changed"),
        ) from exc
    except ConflictingOptionsError as exc:
        raise AppError(
            "INCOMPATIBLE_OPTIONS", ExitCode.USAGE_ERROR, t("llm.run.error.conflicting_options")
        ) from exc
    except LookupError as exc:
        raise _not_found(LlmRunNotFoundError(request.run_id)) from exc


def _crashed(workspace: Workspace, run_id: object) -> bool:
    """True when the run is still marked running while no writer holds the workspace."""
    try:
        with workspace.lock():
            connection = open_database(workspace)
            try:
                status = connection.execute(
                    "SELECT status FROM llm_runs WHERE id = ?", (run_id,)
                ).fetchone()
            finally:
                connection.close()
            return status is not None and status[0] == RunStatus.RUNNING
    except (WorkspaceBusyError, OSError):
        return False  # a writer is active, or the lock cannot be probed (read-only)


def _not_found(exc: LlmRunNotFoundError) -> AppError:
    if exc.run_id is None:
        return AppError("NO_LLM_RUNS", ExitCode.USAGE_ERROR, t("llm.run.error.no_runs"))
    return AppError(
        "LLM_RUN_NOT_FOUND", ExitCode.USAGE_ERROR, t("llm.run.error.not_found", run_id=exc.run_id)
    )


def _event_sink(stack: ExitStack, target: str | None) -> Callable[[Event], None] | None:
    if target is None:
        return None
    handle: TextIO
    if target == "-":
        handle = sys.stderr
    else:
        handle = stack.enter_context(Path(target).open("a", encoding="utf-8"))  # noqa: SIM115

    def write(event: Event) -> None:
        handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")
        handle.flush()

    return write


def _progress(
    stack: ExitStack, state: CliState, connection: sqlite3.Connection, run_id: int
) -> Callable[[], None] | None:
    if state.json_output or not stderr_console().is_terminal:
        return None
    bar = stack.enter_context(Progress(console=stderr_console(), transient=True))
    task: list[TaskID] = []

    def update() -> None:
        counts = dict(
            connection.execute(
                "SELECT status, count(*) FROM llm_jobs WHERE run_id = ? GROUP BY status", (run_id,)
            ).fetchall()
        )
        total = sum(counts.values())
        done = (
            total
            - counts.get("pending", 0)
            - counts.get("in_flight", 0)
            - counts.get("retry_wait", 0)
        )
        description = t(
            "llm.run.progress",
            done=done,
            total=total,
            failed=counts.get("failed", 0),
            reconcile=counts.get("awaiting_reconciliation", 0),
        )
        if not task:
            task.append(bar.add_task(description, total=total))
        bar.update(task[0], completed=done, description=description)

    return update


def _failure(status: RunStatus, report: dict[str, object]) -> AppError | None:
    if status is RunStatus.COMPLETED:
        return None
    run_info = report["run"]
    assert isinstance(run_info, dict)
    key = {
        RunStatus.COMPLETED_WITH_FAILURES: "COMPLETED_WITH_FAILURES",
        RunStatus.BUDGET_REACHED: "BUDGET_REACHED",
        RunStatus.PAUSED: "RUN_PAUSED_ACCOUNT_ERROR",
        RunStatus.INTERRUPTED: "RUN_INTERRUPTED",
        RunStatus.RUNNING: "RUN_INTERRUPTED",
    }[status]
    return AppError(
        key,
        EXIT_FOR_STATUS[status],
        t(f"llm.run.outcome.{key}", reason=run_info["stop_reason"] or "-"),
    )


def _render(data: dict[str, object]) -> None:
    console = stdout_console()
    run_info, jobs, cost, usage = data["run"], data["jobs"], data["cost"], data["usage"]
    assert isinstance(run_info, dict) and isinstance(jobs, dict)
    assert isinstance(cost, dict) and isinstance(usage, dict)
    if "mode" in data:
        console.print(t(f"llm.run.mode.{data['mode']}", run_id=run_info["id"]), highlight=False)
    table = Table(
        title=t("llm.status.title", run_id=run_info["id"]), title_justify="left", show_header=False
    )
    table.add_column()
    table.add_column(justify="right")
    table.add_row(t("llm.status.row.status"), str(run_info["status"]))
    for name in ("succeeded", "reused", "failed", "awaiting_reconciliation", "skipped", "pending"):
        table.add_row(t(f"llm.status.job.{name}"), f"{jobs.get(name, 0):,}")
    estimated = usage["estimated"]
    reported = usage["reported"]
    assert isinstance(estimated, dict) and isinstance(reported, dict)
    for label, values in (("reported", reported), ("estimated", estimated)):
        parts = [values[b] for b in ("input_uncached", "input_cache_read", "input_cache_write")]
        output = values["output_total"]
        known = all(isinstance(p, int) for p in parts) and isinstance(output, int)
        table.add_row(
            t(f"llm.status.row.tokens_{label}"),
            f"{sum(p for p in parts if isinstance(p, int)):,} / {output:,}" if known else "-",
        )
    known_cost = "-" if cost["known_total"] is None else f"{cost['known_total']} {cost['currency']}"
    if cost["billed_attempts_without_cost"]:
        known_cost += " " + t("llm.status.partial_cost", count=cost["billed_attempts_without_cost"])
    table.add_row(t("llm.status.row.cost"), known_cost)
    reservations, budget = data["reservations"], data["budget"]
    assert isinstance(reservations, dict) and isinstance(budget, dict)
    table.add_row(t("llm.status.row.in_flight"), f"{reservations['in_flight_tokens']:,}")
    table.add_row(t("llm.status.row.unknown"), f"{reservations['unknown_billing_tokens']:,}")
    limit = budget["max_tokens"]
    committed = f"{budget['committed_tokens']:,}" + (f" / {limit:,}" if limit is not None else "")
    table.add_row(t("llm.status.row.committed_tokens"), committed)
    if budget["max_cost"] is not None:
        table.add_row(
            t("llm.status.row.committed_cost"),
            f"{budget['committed_cost'] or '-'} / {budget['max_cost']}",
        )
    console.print(table)
    if data.get("stale"):
        stderr_console().print(t("llm.status.stale"), style="yellow", highlight=False)
    stderr_console().print(t("llm.status.note"), style="dim", highlight=False)
