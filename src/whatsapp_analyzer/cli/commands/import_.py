from collections import Counter
from collections.abc import Callable
from contextlib import ExitStack
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.progress import Progress, TaskID
from rich.table import Table

from whatsapp_analyzer.cli.output import (
    CliState,
    print_error,
    result_payload,
    stderr_console,
    stdout_console,
    write_json,
)
from whatsapp_analyzer.errors import AppError
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.i18n import t
from whatsapp_analyzer.ingest.csv_export import NormalizedCsvWriter
from whatsapp_analyzer.ingest.mapping import (
    PRESETS,
    ColumnMapping,
    load_mapping_file,
    parse_overrides,
)
from whatsapp_analyzer.ingest.model import SEVERITY, InputError, InputFormat, Message, Severity
from whatsapp_analyzer.ingest.pipeline import ImportOptions, run_import
from whatsapp_analyzer.ingest.report import FileReport, ImportReport
from whatsapp_analyzer.ingest.whatsapp_txt import DateOrder


class FormatChoice(StrEnum):
    AUTO = "auto"
    IMAZING_TXT = InputFormat.IMAZING_TXT.value
    WHATSAPP_TXT = InputFormat.WHATSAPP_TXT.value
    CSV = InputFormat.CSV.value
    XLSX = InputFormat.XLSX.value


def import_command(
    ctx: typer.Context,
    paths: Annotated[list[Path], typer.Argument(metavar="PATH...", help=t("import.arg.paths"))],
    input_format: Annotated[
        FormatChoice, typer.Option("--format", help=t("import.option.format"))
    ] = FormatChoice.AUTO,
    owner: Annotated[str | None, typer.Option(help=t("import.option.owner"))] = None,
    date_order: Annotated[
        DateOrder | None, typer.Option(help=t("import.option.date_order"))
    ] = None,
    mapping: Annotated[
        str | None, typer.Option(metavar="FILE|PRESET", help=t("import.option.mapping"))
    ] = None,
    column: Annotated[
        list[str] | None,
        typer.Option("--map", metavar="FIELD=COLUMN", help=t("import.option.map")),
    ] = None,
    sheet: Annotated[str | None, typer.Option(help=t("import.option.sheet"))] = None,
    header_row: Annotated[
        int | None, typer.Option(min=1, help=t("import.option.header_row"))
    ] = None,
    encoding: Annotated[str | None, typer.Option(help=t("import.option.encoding"))] = None,
    delimiter: Annotated[str | None, typer.Option(help=t("import.option.delimiter"))] = None,
    normalized_out: Annotated[
        Path | None, typer.Option(metavar="FILE", help=t("import.option.normalized_out"))
    ] = None,
    overwrite: Annotated[bool, typer.Option(help=t("import.option.overwrite"))] = False,
    drop_duplicates: Annotated[bool, typer.Option(help=t("import.option.drop_duplicates"))] = False,
    strict: Annotated[bool, typer.Option(help=t("import.option.strict"))] = False,
    show_file_names: Annotated[bool, typer.Option(help=t("import.option.show_file_names"))] = False,
) -> None:
    state: CliState = ctx.obj
    options = ImportOptions(
        format=None if input_format is FormatChoice.AUTO else InputFormat(input_format.value),
        owner=owner,
        date_order=date_order,
        mapping=_load_mapping(mapping),
        column_overrides=_parse_overrides(column or []),
        sheet=sheet,
        header_row=header_row,
        encoding=encoding,
        delimiter=delimiter,
        drop_duplicates=drop_duplicates,
    )
    if normalized_out is not None:
        _check_output(normalized_out, overwrite)

    report, written = _run(paths, options, normalized_out, show_progress=not state.json_output)
    failure = _failure(report, strict)

    if state.json_output:
        data = report.to_dict(include_file_names=show_file_names)
        data["output"] = (
            {"path": str(normalized_out), "records": written} if written is not None else None
        )
        write_json(result_payload("import", data, failure))
    else:
        _render(report, show_file_names, normalized_out, written, failure)
    if failure is not None:
        raise typer.Exit(failure.exit_code)


def _run(
    paths: list[Path], options: ImportOptions, output: Path | None, show_progress: bool
) -> tuple[ImportReport, int | None]:
    """Import, writing the normalized CSV only if every file is accepted."""
    with ExitStack() as stack:
        writer = None
        if output is not None:
            try:
                writer = stack.enter_context(NormalizedCsvWriter(output))
            except OSError as exc:
                raise _output_error(output, exc) from exc
        progress = None
        if show_progress and stderr_console().is_terminal:
            progress = stack.enter_context(Progress(console=stderr_console(), transient=True))
        try:
            report = run_import(
                paths,
                options,
                sink=_guarded_sink(writer) if writer else None,
                progress=_progress_callback(progress) if progress else None,
            )
            if writer is None or report.rejected_files or not report.files:
                return report, None
            writer.commit()
        except (_OutputFailure, OSError) as exc:
            assert output is not None
            cause = exc.cause if isinstance(exc, _OutputFailure) else exc
            raise _output_error(output, cause) from exc
        return report, writer.rows


class _OutputFailure(Exception):
    """Writing the normalized CSV failed; kept apart from errors reading the inputs."""

    def __init__(self, cause: OSError) -> None:
        super().__init__(str(cause))
        self.cause = cause


def _guarded_sink(writer: NormalizedCsvWriter) -> Callable[[Message], None]:
    def write(message: Message) -> None:
        try:
            writer.write(message)
        except OSError as exc:
            raise _OutputFailure(exc) from exc

    return write


def _output_error(path: Path, exc: OSError) -> AppError:
    return AppError(
        "OUTPUT_NOT_WRITABLE",
        ExitCode.OUTPUT_ERROR,
        t("import.error.not_writable", path=path, reason=exc.strerror or type(exc).__name__),
    )


def _progress_callback(progress: Progress) -> Callable[[int, int, int], None]:
    task: list[TaskID] = []

    def update(index: int, total: int, records: int) -> None:
        description = t("import.progress", index=index, total=total, records=records)
        if not task:
            task.append(progress.add_task(description, total=total))
        progress.update(task[0], completed=index - 1, description=description)

    return update


def _load_mapping(value: str | None) -> ColumnMapping | None:
    if value is None:
        return None
    if value in PRESETS:
        return PRESETS[value]
    try:
        return load_mapping_file(Path(value))
    except InputError as error:
        raise AppError(
            error.code.value,
            ExitCode.USAGE_ERROR,
            _input_error_message(error.code.value, error.details),
            hint=t("import.hint.mapping"),
        ) from error


def _parse_overrides(pairs: list[str]) -> dict[str, str]:
    try:
        return parse_overrides(pairs)
    except InputError as error:
        raise AppError(
            error.code.value,
            ExitCode.USAGE_ERROR,
            t("import.error.invalid_override", value=error.details.get("override", "")),
            hint=t("import.hint.mapping"),
        ) from error


def _check_output(path: Path, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise AppError(
            "OUTPUT_EXISTS",
            ExitCode.USAGE_ERROR,
            t("import.error.output_exists", path=path),
            hint=t("import.hint.overwrite"),
        )
    if path.is_dir():
        raise AppError(
            "OUTPUT_NOT_WRITABLE",
            ExitCode.OUTPUT_ERROR,
            t("import.error.output_is_directory", path=path),
        )


def _failure(report: ImportReport, strict: bool) -> AppError | None:
    if not report.files:
        return AppError(
            "NO_INPUT_FILES",
            ExitCode.USAGE_ERROR,
            t("import.error.no_inputs"),
            hint=t("import.hint.no_inputs"),
        )
    rejected = report.rejected_files
    if rejected:
        codes = {f.error.value for f in rejected if f.error}
        code = codes.pop() if len(codes) == 1 else "VALIDATION_FAILED"
        return AppError(
            code,
            ExitCode.VALIDATION_FAILED,
            t("import.error.rejected", rejected=len(rejected), total=len(report.files)),
            hint=t("import.hint.rejected"),
        )
    invalid = sum(f.records_invalid for f in report.files)
    if strict and invalid:
        return AppError(
            "INVALID_RECORDS", ExitCode.VALIDATION_FAILED, t("import.error.strict", invalid=invalid)
        )
    return None


def _input_error_message(code: str, details: dict[str, object]) -> str:
    """Localized reason plus technical details (field names, counts; never message content)."""
    message = t(f"import.input_error.{code}")
    if not details:
        return message
    rendered = ", ".join(
        f"{key}: {', '.join(map(str, value)) if isinstance(value, list) else value}"
        for key, value in details.items()
    )
    return f"{message} ({rendered})"


def _render(
    report: ImportReport,
    show_file_names: bool,
    output: Path | None,
    written: int | None,
    failure: AppError | None,
) -> None:
    console = stdout_console()
    totals = report.totals()
    summary = Table(title=t("import.summary.title"), title_justify="left", show_header=False)
    summary.add_column()
    summary.add_column(justify="right")
    for key in (
        "files_inspected",
        "files_accepted",
        "files_rejected",
        "records_read",
        "records_valid",
        "records_invalid",
        "duplicates",
        "conversations",
    ):
        summary.add_row(t(f"import.summary.{key}"), f"{totals[key]:,}")
    if date_range := report.date_range():
        summary.add_row(
            t("import.summary.period"), f"{date_range[0]:%Y-%m-%d} - {date_range[1]:%Y-%m-%d}"
        )
    formats = Counter(f.format.value for f in report.accepted_files if f.format)
    if formats:
        summary.add_row(
            t("import.summary.formats"), ", ".join(f"{k} ({v})" for k, v in sorted(formats.items()))
        )
    console.print(summary)

    if issues := report.issue_counts():
        table = Table(title=t("import.issues.title"), title_justify="left")
        for name in ("code", "severity", "count", "meaning"):
            table.add_column(t(f"import.issues.column.{name}"))
        for code, count in sorted(
            issues.items(), key=lambda item: (SEVERITY[item[0]] is not Severity.ERROR, item[0])
        ):
            table.add_row(
                code.value,
                t(f"import.severity.{SEVERITY[code].value}"),
                f"{count:,}",
                t(f"import.issue.{code.value}"),
            )
        console.print(table)

    if rejected := report.rejected_files:
        table = Table(title=t("import.rejected.title"), title_justify="left")
        for name in ("file", "code", "reason"):
            table.add_column(t(f"import.rejected.column.{name}"))
        for file_report in rejected:
            table.add_row(_file_label(file_report, show_file_names), *_rejection(file_report))
        console.print(table)

    if written is not None:
        console.print(t("import.written", records=written, path=output), highlight=False)
    elif output is not None and failure is not None:
        console.print(t("import.not_written"), style="yellow", highlight=False)
    if failure is not None:
        print_error(failure)


def _file_label(report: FileReport, show_name: bool) -> str:
    return report.name if show_name else t("import.file_label", index=report.index)


def _rejection(report: FileReport) -> tuple[str, str]:
    assert report.error is not None
    code = report.error.value
    return code, _input_error_message(code, report.error_details)
