from pathlib import Path
from typing import Annotated

import typer

from whatsapp_analyzer.cli.output import CliState, result_payload, stdout_console, write_json
from whatsapp_analyzer.cli.workspace_support import database_error, workspace_error
from whatsapp_analyzer.errors import AppError
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.i18n import t
from whatsapp_analyzer.store.db import DatabaseError, schema_version
from whatsapp_analyzer.workspace import WorkspaceError, init_workspace


def init(
    ctx: typer.Context,
    directory: Annotated[
        Path, typer.Argument(metavar="DIR", help=t("init.arg.directory"))
    ] = Path(),
) -> None:
    state: CliState = ctx.obj
    try:
        workspace = init_workspace(directory)
    except WorkspaceError as exc:
        raise workspace_error(exc) from exc
    except OSError as exc:
        raise AppError(
            "OUTPUT_NOT_WRITABLE",
            ExitCode.OUTPUT_ERROR,
            t(
                "workspace.error.not_writable",
                path=directory,
                reason=exc.strerror or type(exc).__name__,
            ),
        ) from exc
    try:
        connection = workspace.connect()
    except DatabaseError as exc:
        raise database_error(exc, workspace.database_path) from exc
    try:
        version = schema_version(connection)
    finally:
        connection.close()

    data: dict[str, object] = {
        "workspace": str(workspace.root),
        "config": str(workspace.config_path),
        "database": str(workspace.database_path),
        "schema_version": version,
    }
    if state.json_output:
        write_json(result_payload("init", data))
        return
    console = stdout_console()
    console.print(t("init.created", path=workspace.root), highlight=False)
    console.print(t("init.next_steps", path=workspace.root), highlight=False, style="dim")
