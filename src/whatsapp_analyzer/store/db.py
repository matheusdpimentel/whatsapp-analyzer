"""Connections, pragmas, migrations and explicit transactions.

Connections use autocommit mode (`isolation_level=None`) and every write goes through
`transaction()`, so it is always visible where a transaction starts and ends. The database runs
in WAL mode: readers (status, export) see a consistent snapshot while a writer commits batches.
"""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from whatsapp_analyzer.store.migrations import LATEST_VERSION, MIGRATIONS

APPLICATION_ID = 0x57434131  # "WCA1": marks files created by this tool
BUSY_TIMEOUT_MS = 5000


class DatabaseError(Exception):
    """The file is not a usable analyzer database. `reason` is a stable identifier."""

    def __init__(self, reason: str, **details: object) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


def create_database(path: Path) -> None:
    """Create a new database file readable only by its owner and apply all migrations."""
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    connection = connect(path)
    connection.close()


def connect(path: Path) -> sqlite3.Connection:
    """Open an existing analyzer database and bring its schema up to date."""
    if not path.is_file():
        raise DatabaseError("DATABASE_NOT_FOUND")
    connection = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        connection.row_factory = sqlite3.Row
        _check_identity(connection)
        _enable_wal(connection)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    except sqlite3.DatabaseError as exc:
        connection.close()
        raise DatabaseError("NOT_A_DATABASE") from exc
    except BaseException:
        connection.close()
        raise
    try:
        migrate(connection)
    except sqlite3.Error as exc:
        connection.close()
        raise DatabaseError("MIGRATION_FAILED", error=type(exc).__name__) from exc
    except BaseException:
        connection.close()
        raise
    return connection


def _enable_wal(connection: sqlite3.Connection) -> None:
    mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        raise DatabaseError("WAL_UNAVAILABLE")


def _check_identity(connection: sqlite3.Connection) -> None:
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    is_empty = connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    if application_id != APPLICATION_ID and not (application_id == 0 and is_empty):
        raise DatabaseError("NOT_AN_ANALYZER_DATABASE")
    if version > LATEST_VERSION:
        raise DatabaseError("DATABASE_TOO_NEW", version=version, supported=LATEST_VERSION)


def schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def migrate(connection: sqlite3.Connection) -> None:
    """Apply pending migrations one per transaction. The version is read again after taking the
    write lock, so two processes opening an old database never apply the same migration."""
    while schema_version(connection) < len(MIGRATIONS):
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = schema_version(connection)
            if current < len(MIGRATIONS):
                for statement in _statements(MIGRATIONS[current]):
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={current + 1}")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")


def _statements(script: str) -> Iterator[str]:
    """Split a migration into statements (migrations contain no semicolons inside literals).
    A trigger body holds semicolons, so pieces are joined until SQLite sees a complete one."""
    pending = ""
    for piece in script.split(";"):
        pending += piece
        if not pending.strip():
            pending = ""
        elif sqlite3.complete_statement(pending + ";"):
            yield pending
            pending = ""
        else:
            pending += ";"


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A write transaction. `BEGIN IMMEDIATE` takes the write lock up front, so a conflict
    surfaces at the start instead of in the middle of the work."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


@contextmanager
def read_snapshot(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A read transaction: every query inside sees the same committed state (WAL snapshot)."""
    connection.execute("BEGIN")
    try:
        yield connection
    finally:
        connection.execute("COMMIT")


def utc_now() -> str:
    """Audit timestamp. Never used in calculations: metrics depend only on the data and the
    configured reference date."""
    return datetime.now(UTC).isoformat(timespec="seconds")
