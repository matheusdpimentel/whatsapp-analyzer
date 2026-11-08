from collections.abc import Callable
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.progress import Progress, TaskID
from rich.table import Table

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
from whatsapp_analyzer.config import ConfigError, parse_reference_date
from whatsapp_analyzer.errors import AppError
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.hashing import short
from whatsapp_analyzer.i18n import t
from whatsapp_analyzer.metrics.reports import RunNotFoundError, compare_runs
from whatsapp_analyzer.metrics.runner import NothingToScoreError, RunResult, run_metrics
from whatsapp_analyzer.metrics.scoring import Level


def run(
    ctx: typer.Context,
    workspace: WorkspaceOption = None,
    config: Annotated[
        Path | None, typer.Option(metavar="FILE", help=t("metrics.option.config"))
    ] = None,
    reference_date: Annotated[
        str | None,
        typer.Option(metavar="YYYY-MM-DD", help=t("metrics.option.reference_date")),
    ] = None,
    force: Annotated[bool, typer.Option(help=t("metrics.option.force"))] = False,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    settings = load_settings(target, config)
    override = _parse_date(reference_date)
    with ExitStack() as stack:
        stack.enter_context(writer_lock(target))
        connection = open_database(target)
        stack.callback(connection.close)
        progress = None
        if not state.json_output and stderr_console().is_terminal:
            progress = stack.enter_context(Progress(console=stderr_console(), transient=True))
        try:
            result = run_metrics(
                connection,
                settings,
                reference_override=override,
                force=force,
                progress=_progress_callback(progress) if progress else None,
            )
        except ConfigError as exc:
            raise config_error(exc) from exc
        except NothingToScoreError as exc:
            raise AppError(
                "NO_CONVERSATIONS",
                ExitCode.USAGE_ERROR,
                t("metrics.error.no_conversations"),
                hint=t("metrics.hint.no_conversations"),
            ) from exc

    data = _result_dict(result)
    if state.json_output:
        write_json(result_payload("metrics run", data))
        return
    console = stdout_console()
    console.print(
        t(
            f"metrics.run.{result.mode.value}",
            run_id=result.run_id,
            reference_date=result.reference_date.isoformat(),
            source=t(f"metrics.reference_source.{result.reference_source.value}"),
        ),
        highlight=False,
    )
    console.print(
        t(
            "metrics.run.details",
            total=result.conversations_total,
            computed=result.metrics_computed,
            reused=result.metrics_reused,
            metrics_hash=short(result.metrics_hash),
            scoring_hash=short(result.scoring_hash),
        ),
        highlight=False,
        style="dim",
    )
    console.print(levels_table(t("metrics.levels.title"), result.levels or {}))


def compare(
    ctx: typer.Context,
    run_a: Annotated[int, typer.Argument(metavar="RUN_A", help=t("metrics.arg.run_a"))],
    run_b: Annotated[int, typer.Argument(metavar="RUN_B", help=t("metrics.arg.run_b"))],
    workspace: WorkspaceOption = None,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    connection = open_database(target)
    try:
        data = compare_runs(connection, run_a, run_b)
    except RunNotFoundError as exc:
        raise run_not_found(exc) from exc
    finally:
        connection.close()

    if state.json_output:
        write_json(result_payload("metrics compare", data))
        return
    _render_comparison(data)


def run_not_found(exc: RunNotFoundError) -> AppError:
    if exc.run_id is None:
        return AppError(
            "NO_RUNS",
            ExitCode.USAGE_ERROR,
            t("metrics.error.no_runs"),
            hint=t("metrics.hint.no_runs"),
        )
    return AppError(
        "RUN_NOT_FOUND",
        ExitCode.USAGE_ERROR,
        t("metrics.error.run_not_found", run_id=exc.run_id),
        hint=t("metrics.hint.status"),
    )


def levels_table(title: str, levels: dict[str, int]) -> Table:
    table = Table(title=title, title_justify="left")
    table.add_column(t("metrics.levels.column.level"))
    table.add_column(t("metrics.levels.column.count"), justify="right")
    for level in Level:
        table.add_row(level.value, f"{levels.get(level.value, 0):,}")
    return table


def _render_comparison(data: dict[str, object]) -> None:
    console = stdout_console()
    run_a, run_b = data["run_a"], data["run_b"]
    assert isinstance(run_a, dict) and isinstance(run_b, dict)
    for side in (run_a, run_b):
        if side["partial"]:
            console.print(t("metrics.compare.partial", run_id=side["id"]), style="yellow")

    summary = Table(title=t("metrics.compare.title"), title_justify="left", show_header=False)
    summary.add_column()
    summary.add_column(justify="right")
    agreement = data["agreement"]
    difference = data["mean_absolute_score_difference"]
    for key, value in (
        ("compared", f"{data['compared']:,}"),
        ("agreement", f"{agreement:.1%}" if isinstance(agreement, float) else "-"),
        ("mean_absolute_score_difference", f"{difference:.4f}" if difference is not None else "-"),
        ("only_in_a", f"{data['only_in_a']:,}"),
        ("only_in_b", f"{data['only_in_b']:,}"),
        ("different_data_version", f"{data['different_data_version']:,}"),
    ):
        summary.add_row(t(f"metrics.compare.{key}"), value)
    changed = data["changed_settings"]
    assert isinstance(changed, list)
    summary.add_row(t("metrics.compare.changed_settings"), ", ".join(changed) or "-")
    console.print(summary)

    transitions = data["transitions"]
    assert isinstance(transitions, dict)
    matrix = Table(
        title=t("metrics.compare.matrix", run_a=run_a["id"], run_b=run_b["id"]),
        title_justify="left",
    )
    matrix.add_column(f"{run_a['id']} \\ {run_b['id']}")
    for level in Level:
        matrix.add_column(level.value, justify="right")
    for level in Level:
        row = transitions[level.value]
        matrix.add_row(level.value, *(f"{row[other.value]:,}" for other in Level))
    console.print(matrix)


def _result_dict(result: RunResult) -> dict[str, object]:
    return {
        "run_id": result.run_id,
        "mode": result.mode.value,
        "reference_date": result.reference_date.isoformat(),
        "reference_date_source": result.reference_source.value,
        "metrics_hash": result.metrics_hash,
        "scoring_hash": result.scoring_hash,
        "conversations": result.conversations_total,
        "metrics_computed": result.metrics_computed,
        "metrics_reused": result.metrics_reused,
        "levels": result.levels,
    }


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return parse_reference_date(value, "--reference-date")
    except ConfigError as exc:
        error = config_error(exc)
        raise AppError(error.code, ExitCode.USAGE_ERROR, error.message) from exc


def _progress_callback(progress: Progress) -> Callable[[int, int], None]:
    task: list[TaskID] = []

    def update(done: int, total: int) -> None:
        description = t("metrics.progress", done=done, total=total)
        if not task:
            task.append(progress.add_task(description, total=total))
        progress.update(task[0], completed=done, description=description)

    return update
