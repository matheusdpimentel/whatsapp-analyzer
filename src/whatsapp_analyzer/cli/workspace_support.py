"""Shared option and error translation for the commands that use a workspace."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer

from whatsapp_analyzer.config import ConfigError, Settings
from whatsapp_analyzer.errors import AppError
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.i18n import t
from whatsapp_analyzer.store.db import DatabaseError
from whatsapp_analyzer.store.lock import WorkspaceBusyError
from whatsapp_analyzer.workspace import Workspace, WorkspaceError, open_workspace

WorkspaceOption = Annotated[
    Path | None,
    typer.Option("--workspace", "-w", metavar="DIR", help=t("workspace.option.workspace")),
]


def resolve_workspace(option: Path | None) -> Workspace:
    try:
        return open_workspace(option)
    except WorkspaceError as exc:
        raise workspace_error(exc) from exc


def workspace_error(exc: WorkspaceError) -> AppError:
    if exc.reason == "WORKSPACE_NOT_FOUND":
        return AppError(
            exc.reason,
            ExitCode.CONFIG_ERROR,
            t("workspace.error.not_found", path=exc.path),
            hint=t("workspace.hint.init"),
        )
    return AppError(
        exc.reason,
        ExitCode.USAGE_ERROR,
        t(f"workspace.error.{exc.reason.lower()}", path=exc.path),
    )


def open_database(workspace: Workspace) -> sqlite3.Connection:
    try:
        return workspace.connect()
    except DatabaseError as exc:
        raise database_error(exc, workspace.database_path) from exc


def database_error(exc: DatabaseError, path: Path) -> AppError:
    return AppError(
        exc.reason,
        ExitCode.CONFIG_ERROR,
        t(f"workspace.database_error.{exc.reason}", path=path, **exc.details),
    )


def load_settings(workspace: Workspace, config_path: Path | None) -> Settings:
    try:
        return workspace.settings(config_path)
    except ConfigError as exc:
        raise config_error(exc) from exc


def config_error(exc: ConfigError) -> AppError:
    return AppError(
        "CONFIG_INVALID" if exc.kind not in {"missing_file", "unreadable"} else "CONFIG_NOT_FOUND",
        ExitCode.CONFIG_ERROR,
        t(f"config.error.{exc.kind}", **exc.details),
        hint=t("config.hint"),
    )


@contextmanager
def writer_lock(workspace: Workspace) -> Iterator[None]:
    try:
        with workspace.lock():
            yield
    except WorkspaceBusyError as exc:
        raise AppError(
            "WORKSPACE_BUSY",
            ExitCode.USAGE_ERROR,
            t("workspace.error.busy", path=workspace.root),
            hint=t("workspace.hint.busy"),
        ) from exc
