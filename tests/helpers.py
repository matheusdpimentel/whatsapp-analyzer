"""Small builders for workspace tests. All data is synthetic and written by hand."""

import csv
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from whatsapp_analyzer.cli.app import main
from whatsapp_analyzer.store.db import connect
from whatsapp_analyzer.workspace import DATABASE_FILE, init_workspace

HEADER = (
    "conversation_id",
    "timestamp",
    "sender",
    "direction",
    "text",
    "message_type",
    "media_kind",
)

Row = tuple[str, str, str, str, str, str, str]


def row(
    conversation: str,
    timestamp: str,
    direction: str,
    text: str = "hello",
    sender: str | None = None,
    message_type: str = "text",
    media_kind: str = "",
) -> Row:
    if sender is None:
        sender = "owner" if direction == "outbound" else f"{conversation} contact"
    return (conversation, timestamp, sender, direction, text, message_type, media_kind)


def write_csv(path: Path, rows: Sequence[Row]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(HEADER)
        writer.writerows(rows)
    return path


def exchange(conversation: str, day: int, replies: bool = True) -> list[Row]:
    """A short two-way exchange on 2026-01-<day>."""
    rows = [row(conversation, f"2026-01-{day:02d}T10:00:00", "outbound", "Hi there")]
    if replies:
        rows.append(row(conversation, f"2026-01-{day:02d}T10:05:00", "inbound", "ok"))
    return rows


def new_workspace(tmp_path: Path, name: str = "ws") -> Path:
    return init_workspace(tmp_path / name).root


def database(workspace: Path) -> sqlite3.Connection:
    return connect(workspace / DATABASE_FILE)


def count(workspace: Path, table: str) -> int:
    connection = database(workspace)
    try:
        return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def run_cli(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, object]]:
    code = main(["--json", *args])
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    return code, payload


def data_of(payload: dict[str, object]) -> dict[str, object]:
    data = payload["data"]
    assert isinstance(data, dict)
    return data


LOOPBACK_HOSTS = "127.0.0.1,::1"


def socket_policy(platform: str) -> dict[str, object]:
    """How pytest-socket guards the network on `platform`.

    Everywhere, tests may not reach any external host. On POSIX that is `--disable-socket`
    (Unix sockets stay allowed for asyncio's self-pipe). On Windows asyncio's event loop
    creates its self-pipe with `socket.socketpair()`, which is a TCP pair on 127.0.0.1, so
    sockets cannot be disabled outright there: they are allowed, but a connection to anything
    except the loopback addresses is still blocked.
    """
    if platform == "win32":
        return {"disable_socket": False, "allow_hosts": LOOPBACK_HOSTS}
    return {"disable_socket": True, "allow_hosts": None}
