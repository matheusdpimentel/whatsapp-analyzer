from pathlib import Path
from typing import Annotated

import typer

from whatsapp_analyzer.cli.commands.metrics import run_not_found
from whatsapp_analyzer.cli.output import (
    CliState,
    result_payload,
    stderr_console,
    stdout_console,
    write_json,
)
from whatsapp_analyzer.cli.workspace_support import (
    WorkspaceOption,
    open_database,
    resolve_workspace,
)
from whatsapp_analyzer.errors import AppError
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.i18n import t
from whatsapp_analyzer.metrics.reports import RunNotFoundError, export_run


def export(
    ctx: typer.Context,
    out: Annotated[Path, typer.Option(metavar="FILE", help=t("export.option.out"))],
    workspace: WorkspaceOption = None,
    run_id: Annotated[
        int | None, typer.Option("--run", metavar="ID", help=t("export.option.run"))
    ] = None,
    overwrite: Annotated[bool, typer.Option(help=t("export.option.overwrite"))] = False,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    metadata_path = out.with_name(out.name + ".meta.json")
    for path in (out, metadata_path):
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
        result = export_run(connection, run_id, out)
    except RunNotFoundError as exc:
        raise run_not_found(exc) from exc
    except OSError as exc:
        raise AppError(
            "OUTPUT_NOT_WRITABLE",
            ExitCode.OUTPUT_ERROR,
            t("export.error.not_writable", path=out, reason=exc.strerror or type(exc).__name__),
        ) from exc
    finally:
        connection.close()

    meta = result.metadata
    run = meta["run"]
    assert isinstance(run, dict)
    data = {
        "path": str(result.path),
        "metadata_path": str(result.metadata_path),
        "rows": result.rows,
        "run_id": run["id"],
        "run_status": run["status"],
        "partial": meta["partial"],
        "current_data": meta["current_data"],
        "superseded_rows": meta["superseded_rows"],
    }
    if state.json_output:
        write_json(result_payload("export", data))
        return
    stdout_console().print(
        t("export.written", rows=result.rows, run_id=run["id"], path=result.path),
        highlight=False,
    )
    warnings = stderr_console()
    if meta["partial"]:
        warnings.print(t("export.warning.partial", status=run["status"]), style="yellow")
    if not meta["current_data"]:
        warnings.print(
            t("export.warning.outdated", superseded=meta["superseded_rows"]), style="yellow"
        )
