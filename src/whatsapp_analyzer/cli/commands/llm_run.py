"""`llm run`, `llm status` and `llm export`: the persistent runner.

The provider comes from `[llm]` in the configuration: the offline `fake` provider, or a real
one (`openai`, `anthropic`, `openai-compatible`). A real provider is checked completely
(options, capabilities, endpoint, SDK, API key) before anything is planned, and content is
sent only after explicit authorization (`cli.consent`).

Exit codes: 0 completed; 8 completed with failed or unreconciled jobs; 7 budget reached;
5 paused by an account error; 6 interrupted (resumable); 4 invalid provider configuration;
12 authorization required. With `--events FILE` (or `-` for
stderr) one JSON object per event is written, with identifiers and codes only; stdout still
carries exactly one JSON document with `--json`.
"""

import asyncio
import json
import sqlite3
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, NoReturn, TextIO

import typer
from rich.progress import Progress, TaskID
from rich.table import Table

from whatsapp_analyzer.cli import consent
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
from whatsapp_analyzer.llm.call import Generation
from whatsapp_analyzer.llm.catalog import MODELS_FILE, load_catalog
from whatsapp_analyzer.llm.fake import FakeProvider, FakeScenario
from whatsapp_analyzer.llm.pricing import load_prices
from whatsapp_analyzer.llm.prompts import PromptError, find_template
from whatsapp_analyzer.llm.provider import Provider
from whatsapp_analyzer.llm.providers import (
    BuiltProvider,
    ProviderSetupError,
    build_provider,
    is_external,
)
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
    apply_requeue,
    execute,
    open_run,
    requeue_statuses,
    send_estimate,
)
from whatsapp_analyzer.store.db import transaction
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
    authorize_send: Annotated[
        str | None,
        typer.Option(
            "--authorize-send", metavar="DESTINATION", help=t("llm.run.option.authorize_send")
        ),
    ] = None,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    settings = load_settings(target, config)
    args = RunArgs(
        prompt=prompt,
        keys=tuple(conversation) if conversation else None,
        run_id=run_id,
        force=force,
        reuse=reuse,
        retry_failed=retry_failed,
        resend_unknown=resend_unknown,
        overrides=_overrides(concurrency, rpm, tpm, max_tokens, max_cost, max_attempts),
        fake_scenario=fake_scenario,
        events=events,
        authorize_send=authorize_send,
    )
    run_configured(state, target, settings, args, "llm run")


@dataclass(frozen=True)
class RunArgs:
    """Everything `llm run` (and `llm review`, for the reviewer) needs besides the settings."""

    prompt: str | None = None
    keys: tuple[str, ...] | None = None
    versions: tuple[int, ...] | None = None  # a fixed scope (a review of another run)
    run_id: int | None = None
    force: bool = False
    reuse: bool = True
    retry_failed: bool = False
    resend_unknown: bool = False
    overrides: dict[str, Any] = field(default_factory=dict)
    fake_scenario: str | None = None
    events: str | None = None
    authorize_send: str | None = None


def prepare_run(
    target: Workspace, settings: Settings, args: RunArgs
) -> tuple[RunConfig, Provider, BuiltProvider | None]:
    """The run configuration and provider for `settings.llm`, checked before anything is
    planned (a real provider is validated completely; `--authorize-send` must match)."""
    limits = replace(settings.llm.limits, **args.overrides)
    run_config = _run_config(target, settings, args.prompt, limits)
    built: BuiltProvider | None = None
    provider: Provider
    if is_external(settings.llm.provider):
        if args.fake_scenario is not None:
            raise AppError(
                "INCOMPATIBLE_OPTIONS", ExitCode.USAGE_ERROR, t("llm.run.error.fake_scenario_real")
            )
        built, run_config = _real_provider(target, settings, run_config)
        provider = built.provider
    else:
        provider = _provider(args.fake_scenario)
        run_config = replace(run_config, provider=provider.name)
    consent.check_flag(args.authorize_send, run_config.provider)  # before anything is planned
    return run_config, provider, built


def run_configured(
    state: CliState,
    target: Workspace,
    settings: Settings,
    args: RunArgs,
    command: str,
    prepared: tuple[RunConfig, Provider, BuiltProvider | None] | None = None,
    on_opened: Callable[[sqlite3.Connection, OpenedRun], dict[str, object]] | None = None,
) -> NoReturn:
    """Plan or resume, authorize, execute and report one run; exits with the run's code.
    `on_opened` runs under the writer lock once the run exists and this invocation is
    authorized (before it executes); it may add fields to the output."""
    run_config, provider, built = prepared or prepare_run(target, settings, args)
    request = OpenRequest(
        keys=args.keys,
        versions=args.versions,
        run_id=args.run_id,
        force=args.force,
        retry_failed=args.retry_failed,
        resend_unknown=args.resend_unknown,
        limit_overrides=tuple(args.overrides.items()),
    )
    extra: dict[str, object] = {}
    with ExitStack() as stack:
        stack.enter_context(writer_lock(target))
        connection = open_database(target)
        stack.callback(connection.close)
        # With a real provider, explicit re-sends are requeued only after this invocation is
        # authorized: a refused authorization must not leave them pending for the next one.
        opened = _open(connection, run_config, request, defer_requeue=built is not None)
        notices = _notices(built, opened.config)  # a resumed run keeps its price snapshot
        sink = _event_sink(stack, args.events)
        quiet = args.events == "-"  # events on stderr: no progress bar to interleave with
        status = RunStatus(
            connection.execute(
                "SELECT status FROM llm_runs WHERE id = ?", (opened.run_id,)
            ).fetchone()[0]
        )
        authorization = _authorize(
            state, connection, opened, built, args.authorize_send, notices, request
        )
        if built is not None and authorization is not None:
            opened = apply_requeue(connection, opened, args.retry_failed, args.resend_unknown)
        if on_opened is not None:  # only once the run may execute (authorized if needed)
            extra = on_opened(connection, opened)
        if opened.mode in {"new", "resumed"}:
            progress = None if quiet else _progress(stack, state, connection, opened.run_id)
            options = EngineOptions(
                reuse=args.reuse,
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
        "destination": run_config.provider,
        "authorization": authorization,
        "notices": notices,
        **extra,
        **report,
    }
    failure = _failure(status, report)
    if state.json_output:
        write_json(result_payload(command, data, failure))
    else:
        _render(data)
        for notice in notices:
            stderr_console().print(
                t(f"provider.notice.{notice}"), style="yellow", highlight=False, markup=False
            )
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
        generation_settings=Generation(
            max_output_tokens=settings.llm.max_output_tokens,
            temperature=settings.llm.temperature,
        ),
        timeout_seconds=settings.llm.connection.timeout_seconds,
    )


def _authorize(
    state: CliState,
    connection: sqlite3.Connection,
    opened: OpenedRun,
    built: BuiltProvider | None,
    flag_value: str | None,
    notices: list[str],
    request: OpenRequest,
) -> str | None:
    """Authorization of this invocation, when it may send content to a real provider: pending
    jobs, and the jobs that --retry-failed / --resend-unknown would requeue."""
    if built is None:
        return None
    statuses = requeue_statuses(request.retry_failed, request.resend_unknown)
    estimate = send_estimate(connection, opened, statuses)
    if not estimate.jobs:
        return None
    try:
        method = consent.authorize(
            state,
            identity=built.identity,
            model=opened.config.model,
            loopback=built.loopback,
            estimate=estimate,
            flag_value=flag_value,
        )
    except AppError as exc:
        # Nothing was sent. The run is left resumable and NOT `running`: a running run found
        # by the next writer would be taken for a crash.
        with transaction(connection):
            connection.execute(
                "UPDATE llm_runs SET status = 'interrupted', stop_reason = ? "
                "WHERE id = ? AND status = 'running'",
                (exc.code, opened.run_id),
            )
        if exc.details is not None:
            exc.details = {"run_id": opened.run_id, "notices": notices, **exc.details}
        raise
    consent.record(connection, opened.run_id, method, built.identity, opened.config.model)
    return method


def _real_provider(
    workspace: Workspace, settings: Settings, run_config: RunConfig
) -> tuple[BuiltProvider, RunConfig]:
    """Build and check a real provider; the run config gets its identity and parameters."""
    llm = settings.llm
    try:
        info = load_catalog(workspace.root / MODELS_FILE).lookup(llm.provider, llm.model)
    except ConfigError as exc:
        raise config_error(exc) from exc
    try:
        built = build_provider(
            llm.provider,
            llm.model,
            info=info,
            max_output_tokens=run_config.max_output_tokens,
            temperature=llm.temperature,
            options=llm.options,
            api_key_env=llm.connection.api_key_env,
            base_url=llm.connection.base_url,
            timeout_seconds=llm.connection.timeout_seconds,
        )
    except ProviderSetupError as exc:
        raise provider_setup_error(exc) from exc
    generation = replace(run_config.generation_settings, params=built.params)
    return built, replace(
        run_config, provider=built.identity, generation_settings=generation, model_info=info
    )


def provider_setup_error(exc: ProviderSetupError) -> AppError:
    return AppError(
        exc.code,
        ExitCode.CONFIG_ERROR,
        t(f"provider.error.{exc.code}", **exc.details),
        hint=t(f"provider.hint.{exc.code}", **exc.details),
        details={"setup_error": exc.code, **{k: str(v) for k, v in exc.details.items()}},
    )


def _notices(built: BuiltProvider | None, run_config: RunConfig) -> list[str]:
    """Things a person or an agent should know before trusting the numbers of a run."""
    if built is None:
        return []
    notices = list(built.notices)
    if run_config.price is None:
        notices.append("PRICE_MISSING")
    elif run_config.price.stale(date.today()):
        notices.append("PRICE_STALE")
    return notices


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


def _open(
    connection: sqlite3.Connection, config: RunConfig, request: OpenRequest, defer_requeue: bool
) -> OpenedRun:
    try:
        return open_run(connection, config, request, defer_requeue=defer_requeue)
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
            details={
                "provider": config.provider,
                "model": config.model,
                "price": None if config.price is None else "incomplete",
                "needs": ["input", "output", "cache_read", "cache_write"]
                + (
                    ["long_input", "long_cache_read", "long_cache_write", "long_output"]
                    if config.price is not None and config.price.max_input_tokens is not None
                    else []
                ),
            },
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
    review = data.get("review")
    if isinstance(review, dict):  # a review run: its figures are the reviewer's only
        console.print(
            t("llm.review.render.header", run_id=run_info["id"], primary_run=review["primary_run"]),
            highlight=False,
            markup=False,
        )
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
    cost_label = (
        "llm.status.row.cost_reviewer" if isinstance(review, dict) else "llm.status.row.cost"
    )
    table.add_row(t(cost_label), known_cost)
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
