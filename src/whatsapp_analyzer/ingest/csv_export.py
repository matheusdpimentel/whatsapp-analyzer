"""Normalized CSV output in the canonical column order.

Spreadsheet applications execute cells that start with `=`, `+`, `-`, `@`, tab or carriage
return ("CSV injection"). Message text is untrusted, so free-text fields that start with one of
those characters are written with a leading apostrophe, which spreadsheets treat as "show as
text". To keep the export reversible, a value that already starts with an apostrophe followed by
one of those characters (or by another apostrophe) is escaped the same way. The canonical CSV
importer removes exactly one escaping apostrophe, so export -> import round-trips losslessly.
"""

import csv
import os
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

from whatsapp_analyzer.ingest.model import Message
from whatsapp_analyzer.schema import CANONICAL_COLUMNS

FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
_ESCAPABLE_AFTER_APOSTROPHE = (*FORMULA_TRIGGERS, "'")


def escape_formula(value: str) -> str:
    if value.startswith(FORMULA_TRIGGERS) or (
        value.startswith("'") and value[1:2] and value[1] in _ESCAPABLE_AFTER_APOSTROPHE
    ):
        return "'" + value
    return value


def unescape_formula(value: str) -> str:
    if value.startswith("'") and value[1:2] and value[1] in _ESCAPABLE_AFTER_APOSTROPHE:
        return value[1:]
    return value


def message_row(message: Message) -> list[str]:
    return [
        escape_formula(message.conversation_id),
        message.timestamp.isoformat(),
        escape_formula(message.sender),
        message.direction.value if message.direction else "",
        escape_formula(message.text),
        message.message_type.value,
        message.media_kind.value if message.media_kind else "",
    ]


class NormalizedCsvWriter:
    """Writes to a temporary file next to `path` and moves it into place only on success.

    Leaving the `with` block without calling `commit()` removes the temporary file, so a failed
    or rejected import never leaves a partial CSV behind.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: TextIO | None = None
        self._writer: Any = None  # csv writer objects have no public type
        self._committed = False
        self.rows = 0

    def __enter__(self) -> "NormalizedCsvWriter":
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        self._temporary = Path(temporary)
        self._handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
        self._writer = csv.writer(self._handle, lineterminator="\n")
        self._writer.writerow(CANONICAL_COLUMNS)
        return self

    def write(self, message: Message) -> None:
        assert self._writer is not None
        self._writer.writerow(message_row(message))
        self.rows += 1

    def commit(self) -> None:
        assert self._handle is not None
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        self._temporary.replace(self.path)
        self._committed = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if not self._committed:
            if self._handle is not None and not self._handle.closed:
                self._handle.close()
            self._temporary.unlink(missing_ok=True)
