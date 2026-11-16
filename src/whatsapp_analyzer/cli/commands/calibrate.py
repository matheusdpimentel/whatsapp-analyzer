"""`calibrate` commands (milestone 8): human-reviewed examples against model and metric labels.

Everything here is offline: no provider is called, no run, review or score is modified, and no
configuration is written. `sample` and `import` write the calibration set (immutable items and
labels); the other commands read it and export CSV plus `.meta.json` without message text.
`show` prints messages to the terminal only, so a person can label a version locally.
"""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, TypeVar

import typer

from whatsapp_analyzer.calibration import evaluate, sets
from whatsapp_analyzer.calibration import suggest as suggestions
from whatsapp_analyzer.calibration.common import (
    CalibrationError,
    get_set,
    link_for,
    write_export,
)
from whatsapp_analyzer.cli.commands.llm_review import review_error
from whatsapp_analyzer.cli.output import CliState, result_payload, stdout_console, write_json
from whatsapp_analyzer.cli.workspace_support import (
    WorkspaceOption,
    load_settings,
    open_database,
    resolve_workspace,
    writer_lock,
)
from whatsapp_analyzer.errors import AppError
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.fileio import atomic_write_files
from whatsapp_analyzer.i18n import t
from whatsapp_analyzer.runner import review as reviews

T = TypeVar("T")

SetOption = Annotated[str, typer.Option("--set", metavar="NAME", help=t("calibrate.option.set"))]
ConfigOption = Annotated[Path | None, typer.Option(metavar="FILE", help=t("llm.run.option.config"))]
OutOption = Annotated[Path, typer.Option(metavar="FILE", help=t("export.option.out"))]
OptionalOut = Annotated[Path | None, typer.Option(metavar="FILE", help=t("export.option.out"))]
OverwriteOption = Annotated[bool, typer.Option(help=t("export.option.overwrite"))]

_EXIT_CODES = {
    "CALIBRATION_REQUIRED": ExitCode.CALIBRATION_REQUIRED,
    "LABELS_INVALID": ExitCode.VALIDATION_FAILED,
    "LABELS_COLUMNS": ExitCode.VALIDATION_FAILED,
    "LABELS_UNREADABLE": ExitCode.OUTPUT_ERROR,
}


def calibration_error(exc: CalibrationError) -> AppError:
    details = {k: str(v) for k, v in exc.details.items()}
    if exc.code == "LABELS_INVALID":
        problems = exc.details["problems"]
        assert isinstance(problems, list)
        details["first_line"] = str(problems[0]["line"])
        details["first_reason"] = str(problems[0]["reason"])
    return AppError(
        exc.code,
        _EXIT_CODES.get(exc.code, ExitCode.USAGE_ERROR),
        t(f"calibrate.error.{exc.code}", **details),
        hint=t(f"calibrate.hint.{exc.code}") if exc.code == "CALIBRATION_REQUIRED" else None,
        details=exc.details,
    )


def _guard(function: Callable[[], T]) -> T:
    try:
        return function()
    except CalibrationError as exc:
        raise calibration_error(exc) from exc
    except reviews.ReviewError as exc:
        raise review_error(exc) from exc
    except sqlite3.IntegrityError as exc:  # immutability triggers and constraints
        raise AppError(
            "CALIBRATION_REJECTED",
            ExitCode.USAGE_ERROR,
            t("calibrate.error.CALIBRATION_REJECTED", reason=str(exc)),
        ) from exc


def _check_outputs(*paths: Path, overwrite: bool) -> None:
    for path in paths:
        meta = path.with_name(path.name + ".meta.json")
        for target in (path, meta):
            if target.exists() and not overwrite:
                raise AppError(
                    "OUTPUT_EXISTS",
                    ExitCode.USAGE_ERROR,
                    t("export.error.output_exists", path=target),
                    hint=t("export.hint.overwrite"),
                )
        if path.is_dir() or not path.parent.is_dir():
            raise AppError(
                "OUTPUT_NOT_WRITABLE",
                ExitCode.OUTPUT_ERROR,
                t("export.error.invalid_path", path=path),
            )


def _write(action: Callable[[], object], path: Path) -> None:
    try:
        action()
    except OSError as exc:
        raise AppError(
            "OUTPUT_NOT_WRITABLE",
            ExitCode.OUTPUT_ERROR,
            t("export.error.not_writable", path=path, reason=exc.strerror or type(exc).__name__),
        ) from exc


def _say(message: str, /, **values: object) -> None:
    stdout_console().print(t(message, **values), markup=False, highlight=False)


def _caveats(summary: dict[str, object]) -> None:
    caveats = summary.get("caveats")
    assert isinstance(caveats, list)
    for caveat in caveats:
        _say("calibrate.caveat.line", text=t(f"calibrate.caveat.{caveat}"))


def sample(
    ctx: typer.Context,
    set_name: SetOption,
    size: Annotated[int, typer.Option(min=1, metavar="N", help=t("calibrate.sample.option.size"))],
    out: OutOption,
    workspace: WorkspaceOption = None,
    config: ConfigOption = None,
    strategy: Annotated[
        str, typer.Option(metavar="NAME", help=t("calibrate.sample.option.strategy"))
    ] = "representative",
    seed: Annotated[int, typer.Option(metavar="N", help=t("calibrate.sample.option.seed"))] = 0,
    primary_run: Annotated[
        int | None, typer.Option(metavar="ID", help=t("calibrate.option.primary_run"))
    ] = None,
    review_run: Annotated[
        int | None, typer.Option(metavar="ID", help=t("calibrate.option.review_run"))
    ] = None,
    overwrite: OverwriteOption = False,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    llm = load_settings(target, config).llm
    _check_outputs(out, overwrite=overwrite)
    with writer_lock(target):
        connection = open_database(target)
        try:

            def draw() -> sqlite3.Row:
                primary = reviews.primary_run(connection, llm.provider, llm.model, primary_run)
                return sets.create_set(
                    connection, set_name, strategy, size, seed, primary, review_run
                )

            row = _guard(draw)
            meta, meta_path = _write_sheet(connection, row, out, rerun_hint=True)
        finally:
            connection.close()
    data = {"path": str(out), "metadata_path": str(meta_path), **meta}
    if state.json_output:
        write_json(result_payload("calibrate sample", data))
        return
    _say("calibrate.sample.written", size=row["size"], population=row["population"], path=out)
    _say(f"calibrate.sample.kind.{row['sampling']}")


def _write_sheet(
    connection: sqlite3.Connection, row: sqlite3.Row, out: Path, rerun_hint: bool
) -> tuple[dict[str, object], Path]:
    meta = sets.sheet_metadata(row)
    meta_path = out.with_name(out.name + ".meta.json")
    try:
        _write(
            lambda: atomic_write_files(
                (out, sets.sheet_lines(connection, row)),
                (meta_path, [json.dumps(meta, indent=2, sort_keys=True) + "\n"]),
            ),
            out,
        )
    except AppError as exc:
        if rerun_hint:  # the set exists already; only the file is missing
            exc.hint = t("calibrate.hint.SHEET_PENDING", set=row["name"])
            exc.details = {"set": row["name"], "set_created": True}
        raise
    return meta, meta_path


def sheet(
    ctx: typer.Context,
    set_name: SetOption,
    out: OutOption,
    workspace: WorkspaceOption = None,
    overwrite: OverwriteOption = False,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    _check_outputs(out, overwrite=overwrite)
    connection = open_database(target)
    try:
        row = _guard(lambda: get_set(connection, set_name))
        meta, meta_path = _write_sheet(connection, row, out, rerun_hint=False)
    finally:
        connection.close()
    if state.json_output:
        write_json(
            result_payload(
                "calibrate sheet", {"path": str(out), "metadata_path": str(meta_path), **meta}
            )
        )
        return
    _say("calibrate.sheet.written", set=set_name, size=row["size"], path=out)


def import_labels(
    ctx: typer.Context,
    set_name: SetOption,
    labels: Annotated[Path, typer.Option(metavar="FILE", help=t("calibrate.import.option.labels"))],
    workspace: WorkspaceOption = None,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    with writer_lock(target):
        connection = open_database(target)
        try:
            counts = _guard(
                lambda: sets.import_labels(connection, get_set(connection, set_name), labels)
            )
        finally:
            connection.close()
    if state.json_output:
        write_json(result_payload("calibrate import", {"set": set_name, **counts}))
        return
    _say("calibrate.import.done", set=set_name, **counts)


def show(
    ctx: typer.Context,
    set_name: SetOption,
    workspace: WorkspaceOption = None,
    version: Annotated[
        int | None, typer.Option(metavar="ID", help=t("calibrate.show.option.version"))
    ] = None,
    key: Annotated[
        str | None, typer.Option(metavar="KEY", help=t("calibrate.show.option.key"))
    ] = None,
    offset: Annotated[int, typer.Option(min=0, help=t("calibrate.show.option.offset"))] = 0,
    limit: Annotated[int, typer.Option(min=1, help=t("calibrate.show.option.limit"))] = 200,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    connection = open_database(target)
    try:
        result = _guard(
            lambda: sets.show(
                connection,
                get_set(connection, set_name),
                version,
                key,
                offset,
                limit,
                include_text=not state.json_output,  # text is for the terminal, never --json
            )
        )
    finally:
        connection.close()
    if state.json_output:
        write_json(result_payload("calibrate show", result))
        return
    console = stdout_console()
    messages = result["messages"]
    assert isinstance(messages, list)
    _say(
        "calibrate.show.header",
        key=result["conversation_key"],
        version=result["version_id"],
        data_version=result["data_version"],
        shown=len(messages),
        total=result["total_messages"],
    )
    for m in messages:
        console.print(
            f"[{m['seq']}] {m['timestamp']} {m['direction'] or '-'} {m['sender']}: {m['text']}",
            markup=False,
            highlight=False,
        )


def report(
    ctx: typer.Context,
    set_name: SetOption,
    out: OutOption,
    workspace: WorkspaceOption = None,
    config: ConfigOption = None,
    run: Annotated[
        list[int] | None, typer.Option(metavar="ID", help=t("calibrate.report.option.run"))
    ] = None,
    overwrite: OverwriteOption = False,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    settings = load_settings(target, config).calibration
    _check_outputs(out, overwrite=overwrite)
    connection = open_database(target)
    try:

        def build() -> tuple[list[list[str]], dict[str, object]]:
            return evaluate.report(
                connection, get_set(connection, set_name), tuple(run or ()), settings
            )

        rows, summary = _guard(build)
        meta_path = out.with_name(out.name + ".meta.json")
        _write(lambda: write_export(out, evaluate.REPORT_COLUMNS, rows, summary), out)
    finally:
        connection.close()
    data = {"path": str(out), "metadata_path": str(meta_path), **summary}
    if state.json_output:
        write_json(result_payload("calibrate report", data))
        return
    _say("calibrate.report.written", labeled=summary["human_labeled"], path=out)
    _say(f"calibrate.report.kind.{summary['sampling']}")
    raters = summary["raters"]
    assert isinstance(raters, list)
    for r in raters:
        _say(
            "calibrate.report.rater",
            rater=r["rater"],
            accuracy=r["accuracy"] if r["accuracy"] is not None else "-",
            kappa=r["weighted_kappa"] if r["weighted_kappa"] is not None else "-",
            compared=r["compared"],
            abstained=r["abstained"],
            absent=r["absent"],
        )
    _caveats(summary)


def disagreements(
    ctx: typer.Context,
    out: OutOption,
    workspace: WorkspaceOption = None,
    config: ConfigOption = None,
    set_name: Annotated[
        str | None, typer.Option("--set", metavar="NAME", help=t("calibrate.disagreements.set"))
    ] = None,
    primary_run: Annotated[
        int | None, typer.Option(metavar="ID", help=t("calibrate.option.primary_run"))
    ] = None,
    review_run: Annotated[
        int | None, typer.Option(metavar="ID", help=t("calibrate.option.review_run"))
    ] = None,
    overwrite: OverwriteOption = False,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    llm = load_settings(target, config).llm
    _check_outputs(out, overwrite=overwrite)
    connection = open_database(target)
    try:

        def build() -> tuple[list[list[str]], dict[str, object]]:
            chosen = None if set_name is None else get_set(connection, set_name)
            if chosen is not None:
                primary_id, link = chosen["primary_run_id"], link_for(connection, chosen)
                scoring = chosen["scoring_run_id"]  # fixed when the set was drawn
            else:
                primary_id = reviews.primary_run(connection, llm.provider, llm.model, primary_run)[
                    "id"
                ]
                try:
                    link = reviews.latest_review(connection, primary_id, review_run)
                except reviews.ReviewError:
                    if review_run is not None:
                        raise
                    link = None
                scoring = sets.recorded_scoring_run(connection, link)
            return evaluate.disagreements(connection, primary_id, link, chosen, scoring)

        rows, summary = _guard(build)
        meta_path = out.with_name(out.name + ".meta.json")
        _write(lambda: write_export(out, evaluate.DIVERGENCE_COLUMNS, rows, summary), out)
    finally:
        connection.close()
    data = {"path": str(out), "metadata_path": str(meta_path), **summary}
    if state.json_output:
        write_json(result_payload("calibrate disagreements", data))
        return
    _say("calibrate.disagreements.written", n=summary["diverging"], path=out)


def compare(
    ctx: typer.Context,
    set_name: SetOption,
    run: Annotated[list[int], typer.Option(metavar="ID", help=t("calibrate.compare.option.run"))],
    workspace: WorkspaceOption = None,
    config: ConfigOption = None,
    out: OptionalOut = None,
    overwrite: OverwriteOption = False,
) -> None:
    state: CliState = ctx.obj
    if len(run) != 2:
        raise AppError(
            "INVALID_OPTION", ExitCode.USAGE_ERROR, t("calibrate.error.COMPARE_RUNS", n=len(run))
        )
    target = resolve_workspace(workspace)
    settings = load_settings(target, config).calibration
    if out is not None:
        _check_outputs(out, overwrite=overwrite)
    connection = open_database(target)
    try:
        rows, summary = _guard(
            lambda: evaluate.compare(
                connection, get_set(connection, set_name), run[0], run[1], settings
            )
        )
        data: dict[str, object] = dict(summary)
        if out is not None:
            _write(lambda: write_export(out, evaluate.COMPARE_COLUMNS, rows, summary), out)
            data["path"] = str(out)
    finally:
        connection.close()
    if state.json_output:
        write_json(result_payload("calibrate compare", data))
        return
    excluded = summary["excluded_by_reason"]
    assert isinstance(excluded, dict)
    _say("calibrate.compare.summary", paired=summary["paired"], items=summary["items"])
    for reason, n in excluded.items():
        _say("calibrate.compare.excluded", reason=reason, n=n)
    runs = summary["runs"]
    assert isinstance(runs, list)
    for r in runs:
        _say(
            "calibrate.compare.run",
            id=r["id"],
            model=f"{r['provider']}/{r['model']}",
            prompt=r["prompt"],
            accuracy=r["accuracy_on_paired"] if r["accuracy_on_paired"] is not None else "-",
            cost=r["recorded_cost"] if r["recorded_cost"] is not None else "-",
        )
    _caveats(summary)


def suggest(
    ctx: typer.Context,
    set_name: SetOption,
    workspace: WorkspaceOption = None,
    config: ConfigOption = None,
    out: OptionalOut = None,
    overwrite: OverwriteOption = False,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    settings = load_settings(target, config).calibration
    if out is not None:
        _check_outputs(out, overwrite=overwrite)
    connection = open_database(target)
    try:
        rows, summary = _guard(
            lambda: suggestions.suggest(connection, get_set(connection, set_name), settings)
        )
        data: dict[str, object] = dict(summary)
        if out is not None:
            _write(lambda: write_export(out, suggestions.COLUMNS, rows, summary), out)
            data["path"] = str(out)
    finally:
        connection.close()
    if state.json_output:
        write_json(result_payload("calibrate suggest", data))
        return
    _say("calibrate.suggest.notice")
    frontier = summary.get("frontier_in_both_parts")
    if isinstance(frontier, list):
        _say("calibrate.suggest.frontier", policies=", ".join(frontier) or "-")
    thresholds = summary.get("thresholds")
    if isinstance(thresholds, dict):
        best = thresholds["best_on_development"]
        _say(
            "calibrate.suggest.thresholds",
            high=best["high"],
            medium=best["medium"],
            holds=thresholds["improvement_holds_on_validation"],
        )
    _caveats(summary)
