import os
from pathlib import Path

import typer
from rich.table import Table
from rich.text import Text

from whatsapp_analyzer import diagnostics
from whatsapp_analyzer.cli.output import CliState, result_payload, stdout_console, write_json
from whatsapp_analyzer.diagnostics import Check, Status
from whatsapp_analyzer.errors import AppError
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.i18n import t

_STATUS_STYLES = {
    Status.OK: "green",
    Status.FAIL: "bold red",
    Status.INFO: "dim",
}


def doctor(ctx: typer.Context) -> None:
    state: CliState = ctx.obj
    checks = diagnostics.run_checks(Path(), os.environ)
    summary = diagnostics.summarize(checks)
    failure = None
    if summary[Status.FAIL]:
        failure = AppError(
            "ENVIRONMENT_CHECK_FAILED", ExitCode.ENVIRONMENT_CHECK_FAILED, t("doctor.failed")
        )

    if state.json_output:
        data: dict[str, object] = {
            "checks": [_check_to_dict(check) for check in checks],
            "summary": summary,
        }
        write_json(result_payload("doctor", data, failure))
    else:
        _render_table(checks, summary)

    if failure is not None:
        raise typer.Exit(failure.exit_code)


def _check_to_dict(check: Check) -> dict[str, object]:
    return {
        "id": check.id,
        "status": check.status.value,
        "value": check.value,
        "message": t(check.message_key, **check.params),
    }


def _render_table(checks: list[Check], summary: dict[str, int]) -> None:
    table = Table(title=t("doctor.title"), title_justify="left")
    table.add_column(t("doctor.column.check"))
    table.add_column(t("doctor.column.status"))
    table.add_column(t("doctor.column.details"), overflow="fold")
    for check in checks:
        table.add_row(
            t(f"doctor.check.{check.id}.label"),
            Text(t(f"doctor.status.{check.status.value}"), style=_STATUS_STYLES[check.status]),
            Text(t(check.message_key, **check.params)),
        )
    console = stdout_console()
    console.print(table)
    console.print(t("doctor.summary", **summary), highlight=False)
