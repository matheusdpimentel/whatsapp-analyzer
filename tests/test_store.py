"""Database creation, migrations, transactions, workspace layout and the writer lock."""

import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from whatsapp_analyzer.store import db
from whatsapp_analyzer.store.db import (
    APPLICATION_ID,
    DatabaseError,
    connect,
    create_database,
    schema_version,
    transaction,
)
from whatsapp_analyzer.store.lock import WorkspaceBusyError, WorkspaceLock
from whatsapp_analyzer.store.migrations import LATEST_VERSION
from whatsapp_analyzer.workspace import (
    WorkspaceError,
    init_workspace,
    open_workspace,
)


def test_new_database_is_migrated_and_marked(tmp_path: Path) -> None:
    path = tmp_path / "analyzer.db"
    create_database(path)
    connection = connect(path)
    try:
        assert schema_version(connection) == LATEST_VERSION
        assert connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "conversations",
            "conversation_versions",
            "messages",
            "conversation_metrics",
            "conversation_scores",
            "scoring_runs",
            "metric_configs",
            "imports",
            "sources",
        } <= tables
    finally:
        connection.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_database_is_private(tmp_path: Path) -> None:
    path = tmp_path / "analyzer.db"
    create_database(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "analyzer.db"
    create_database(path)
    for _ in range(2):
        connect(path).close()
    connection = connect(path)
    assert schema_version(connection) == LATEST_VERSION
    connection.close()


def test_failed_migration_leaves_no_partial_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "analyzer.db"
    sqlite3.connect(path).close()
    monkeypatch.setattr(db, "MIGRATIONS", ("CREATE TABLE a (x); CREATE TABLE broken (",))
    with pytest.raises(DatabaseError) as info:
        connect(path)
    assert info.value.reason == "MIGRATION_FAILED"
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
    raw.close()


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "analyzer.db"
    create_database(path)
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version={LATEST_VERSION + 1}")
    raw.close()
    with pytest.raises(DatabaseError) as info:
        connect(path)
    assert info.value.reason == "DATABASE_TOO_NEW"


def test_foreign_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "other.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE something (x)")
    raw.close()
    with pytest.raises(DatabaseError) as info:
        connect(path)
    assert info.value.reason == "NOT_AN_ANALYZER_DATABASE"


def test_non_database_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "analyzer.db"
    path.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(DatabaseError) as info:
        connect(path)
    assert info.value.reason == "NOT_A_DATABASE"


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    path = tmp_path / "analyzer.db"
    create_database(path)
    connection = connect(path)
    with pytest.raises(RuntimeError), transaction(connection):
        connection.execute(
            "INSERT INTO metric_configs VALUES ('h', 1, '{}', '2026-01-01T00:00:00+00:00')"
        )
        raise RuntimeError
    assert connection.execute("SELECT count(*) FROM metric_configs").fetchone()[0] == 0
    connection.close()


def test_workspace_layout(tmp_path: Path) -> None:
    workspace = init_workspace(tmp_path / "new" / "ws")
    assert workspace.config_path.is_file() and workspace.database_path.is_file()
    assert (workspace.root / ".gitignore").read_text().splitlines()[-1] == "*"
    if sys.platform != "win32":
        assert stat.S_IMODE(workspace.root.stat().st_mode) == 0o700
    with pytest.raises(WorkspaceError) as info:
        init_workspace(workspace.root)
    assert info.value.reason == "WORKSPACE_EXISTS"


def test_workspace_resolution_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = init_workspace(tmp_path / "explicit").root
    from_env = init_workspace(tmp_path / "env").root
    in_cwd = init_workspace(tmp_path / "cwd").root
    monkeypatch.chdir(in_cwd)
    assert open_workspace(explicit, {"WCA_WORKSPACE": str(from_env)}).root == explicit
    assert open_workspace(None, {"WCA_WORKSPACE": str(from_env)}).root == from_env
    assert open_workspace(None, {}).root == Path.cwd()
    with pytest.raises(WorkspaceError):
        open_workspace(tmp_path / "missing", {})


def test_writer_lock_is_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "analyzer.lock"
    with WorkspaceLock(path), pytest.raises(WorkspaceBusyError), WorkspaceLock(path):
        pass
    with WorkspaceLock(path):  # released after the first holder exits
        pass


def test_init_refuses_a_non_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    with pytest.raises(WorkspaceError) as info:
        init_workspace(tmp_path / "repo")
    assert info.value.reason == "DIRECTORY_NOT_EMPTY"
    assert not (tmp_path / "repo" / "analyzer.db").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_init_in_an_existing_empty_directory_makes_it_private(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir(mode=0o755)
    workspace = init_workspace(root)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert (workspace.root / ".gitignore").read_text().splitlines()[-1] == "*"


def test_migrate_on_a_current_database_does_nothing(tmp_path: Path) -> None:
    path = tmp_path / "analyzer.db"
    create_database(path)
    connection = connect(path)
    db.migrate(connection)
    assert schema_version(connection) == LATEST_VERSION
    connection.close()
