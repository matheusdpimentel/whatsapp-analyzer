"""A workspace is a local directory holding one analysis: configuration and database.

    <workspace>/
        analyzer.toml   configuration (no secrets)
        analyzer.db     SQLite database with messages, metrics and scores
        analyzer.lock   single-writer lock
        .gitignore      "*": the workspace is never committed, even inside a repository

The database contains message text, so the directory is created readable only by its owner.
"""

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from whatsapp_analyzer.config import DEFAULT_CONFIG_TEXT, Settings, load_settings
from whatsapp_analyzer.store.db import connect, create_database
from whatsapp_analyzer.store.lock import WorkspaceLock

WORKSPACE_ENV_VAR = "WCA_WORKSPACE"
CONFIG_FILE = "analyzer.toml"
DATABASE_FILE = "analyzer.db"
LOCK_FILE = "analyzer.lock"
GITIGNORE_TEXT = "# Created by whatsapp-analyzer: never commit this workspace.\n*\n"


class WorkspaceError(Exception):
    def __init__(self, reason: str, path: Path) -> None:
        super().__init__(reason)
        self.reason = reason
        self.path = path


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILE

    @property
    def database_path(self) -> Path:
        return self.root / DATABASE_FILE

    def connect(self) -> sqlite3.Connection:
        return connect(self.database_path)

    def settings(self, config_path: Path | None = None) -> Settings:
        return load_settings(config_path or self.config_path)

    def lock(self) -> WorkspaceLock:
        return WorkspaceLock(self.root / LOCK_FILE)


def init_workspace(root: Path) -> Workspace:
    workspace = Workspace(root)
    if workspace.config_path.exists() or workspace.database_path.exists():
        raise WorkspaceError("WORKSPACE_EXISTS", root)
    if root.exists() and not root.is_dir():
        raise WorkspaceError("NOT_A_DIRECTORY", root)
    # Only a new or empty directory: an existing one (for example a repository with its own
    # .gitignore) could expose the database to version control.
    if root.exists() and any(root.iterdir()):
        raise WorkspaceError("DIRECTORY_NOT_EMPTY", root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    (root / ".gitignore").write_text(GITIGNORE_TEXT, encoding="utf-8")
    create_database(workspace.database_path)
    workspace.config_path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    return workspace


def open_workspace(option: Path | None, environ: dict[str, str] | None = None) -> Workspace:
    """Resolve the workspace: `--workspace`, then `WCA_WORKSPACE`, then the current directory."""
    environ = dict(os.environ) if environ is None else environ
    root = option or (Path(environ[WORKSPACE_ENV_VAR]) if environ.get(WORKSPACE_ENV_VAR) else None)
    workspace = Workspace(root or Path.cwd())
    if not (workspace.config_path.is_file() and workspace.database_path.is_file()):
        raise WorkspaceError("WORKSPACE_NOT_FOUND", workspace.root)
    return workspace
