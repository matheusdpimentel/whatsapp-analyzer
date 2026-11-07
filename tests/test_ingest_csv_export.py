import csv
import io
import sys
from datetime import datetime
from pathlib import Path

import pytest

from whatsapp_analyzer.ingest.csv_export import (
    FORMULA_TRIGGERS,
    NormalizedCsvWriter,
    escape_formula,
    unescape_formula,
)
from whatsapp_analyzer.ingest.model import Message
from whatsapp_analyzer.schema import CANONICAL_COLUMNS, Direction, MediaKind, MessageType

MESSAGE = Message(
    "c1",
    datetime(2026, 1, 5, 14, 7, 9),
    "Alex",
    Direction.INBOUND,
    'say "hi",\nthen go',
    MessageType.MEDIA,
    MediaKind.IMAGE,
    7,
)


@pytest.mark.parametrize("trigger", FORMULA_TRIGGERS)
def test_formula_triggers_are_escaped(trigger: str) -> None:
    assert escape_formula(f"{trigger}1+1") == f"'{trigger}1+1"


@pytest.mark.parametrize("value", ["", "plain", "a=1", "'", "'quoted'", "x-1", "5 - 3"])
def test_harmless_values_are_unchanged(value: str) -> None:
    assert escape_formula(value) == value


def test_escaping_round_trips_for_every_short_prefix() -> None:
    alphabet = ["=", "+", "-", "@", "\t", "\r", "'", "a", " ", "1"]
    values = [""] + alphabet + [a + b for a in alphabet for b in alphabet]
    values += [a + b + c for a in alphabet for b in alphabet for c in "'=x"]
    for value in values:
        escaped = escape_formula(value)
        assert unescape_formula(escaped) == value, repr(value)
        assert not escaped.startswith(FORMULA_TRIGGERS), repr(value)


def test_writer_commits_canonical_rows(tmp_path: Path) -> None:
    path = tmp_path / "out.csv"
    with NormalizedCsvWriter(path) as writer:
        writer.write(MESSAGE)
        writer.commit()
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
    assert tuple(rows[0]) == CANONICAL_COLUMNS
    assert rows[1] == [
        "c1",
        "2026-01-05T14:07:09",
        "Alex",
        "inbound",
        'say "hi",\nthen go',
        "media",
        "image",
    ]
    assert writer.rows == 1
    assert list(tmp_path.iterdir()) == [path]


def test_writer_leaves_nothing_behind_without_commit(tmp_path: Path) -> None:
    path = tmp_path / "out.csv"
    with NormalizedCsvWriter(path) as writer:
        writer.write(MESSAGE)
    assert list(tmp_path.iterdir()) == []


def test_writer_leaves_nothing_behind_on_error(tmp_path: Path) -> None:
    path = tmp_path / "out.csv"
    path.write_text("previous content", encoding="utf-8")
    with pytest.raises(RuntimeError), NormalizedCsvWriter(path) as writer:
        writer.write(MESSAGE)
        raise RuntimeError("boom")
    assert [p.name for p in tmp_path.iterdir()] == ["out.csv"]
    assert path.read_text(encoding="utf-8") == "previous content"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_output_is_private_even_when_replacing_a_file(tmp_path: Path) -> None:
    path = tmp_path / "out.csv"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o644)
    with NormalizedCsvWriter(path) as writer:
        writer.write(MESSAGE)
        writer.commit()
    assert path.stat().st_mode & 0o777 == 0o600
