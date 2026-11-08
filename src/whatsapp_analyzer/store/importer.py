"""Persist an import into a workspace database.

Accepted messages are first written to a temporary staging table. Nothing reaches the workspace
unless every input file is accepted; then all conversations are applied in one transaction.

Identity and versions:

- A conversation is identified by `(source_format, conversation_key)`. Formats are separate
  namespaces because exporters name the same chat differently (file name vs. chat title) and a
  chat from one exporter must never silently replace a chat from another.
- Within one import, a conversation key may come from only one file. Two files with the same
  key are rejected instead of being merged.
- Re-importing a conversation with identical content changes nothing. Different content becomes
  a new immutable version and the conversation's current version moves to it; earlier versions,
  their messages and every metric or score computed from them are kept.
- A replacement must share at least one message (timestamp, direction, type and text) with the
  current version. Otherwise it is probably a different chat with the same name, and the whole
  import is refused unless the caller explicitly allows unrelated replacements.
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path

from whatsapp_analyzer import __version__
from whatsapp_analyzer.hashing import canonical_json, format_timestamp
from whatsapp_analyzer.ingest.model import InputError, InputErrorCode, Message
from whatsapp_analyzer.ingest.pipeline import ImportOptions
from whatsapp_analyzer.ingest.report import FileReport, ImportReport
from whatsapp_analyzer.store.db import transaction, utc_now

STAGING_BATCH = 5000

_STAGED_COLUMNS = (
    "file_index, source_format, conversation_key, seq, timestamp, sender, direction, text, "
    "message_type, media_kind"
)


@dataclass(frozen=True)
class _Row:
    file_index: int
    source_format: str
    conversation_key: str
    seq: int
    timestamp: str
    sender: str
    direction: str | None
    text: str
    message_type: str
    media_kind: str | None

    def stored(self) -> tuple[object, ...]:
        return (
            self.seq,
            self.timestamp,
            self.sender,
            self.direction,
            self.text,
            self.message_type,
            self.media_kind,
        )

    def overlap_key(self) -> tuple[object, ...]:
        # Sender is left out: a contact renamed between two exports is still the same chat.
        return (self.timestamp, self.direction, self.message_type, self.text)


@dataclass
class ImportOutcome:
    import_id: int | None = None
    conversations_new: int = 0
    conversations_changed: int = 0
    conversations_unchanged: int = 0
    versions_reactivated: int = 0
    messages_stored: int = 0
    conflicts: list[int] = field(default_factory=list)  # file indexes

    def to_dict(self) -> dict[str, object]:
        return {
            "import_id": self.import_id,
            "conversations_new": self.conversations_new,
            "conversations_changed": self.conversations_changed,
            "conversations_unchanged": self.conversations_unchanged,
            "versions_reactivated": self.versions_reactivated,
            "messages_stored": self.messages_stored,
        }


class ReplacementConflictError(Exception):
    def __init__(self, conversations: int, files: list[int]) -> None:
        super().__init__("CONVERSATION_REPLACEMENT_CONFLICT")
        self.conversations = conversations
        self.files = files


class _NothingChangedError(Exception):
    pass


class WorkspaceImporter:
    def __init__(self, connection: sqlite3.Connection, allow_unrelated: bool = False) -> None:
        self._connection = connection
        self._allow_unrelated = allow_unrelated
        self._started_at = utc_now()
        self._current: tuple[int, str] | None = None
        self._paths: dict[int, Path] = {}
        self._owners: dict[tuple[str, str], int] = {}
        self._next_seq: dict[tuple[str, str], int] = {}
        self._buffer: list[tuple[object, ...]] = []
        connection.execute("DROP TABLE IF EXISTS temp.staged")
        connection.execute(f"CREATE TEMP TABLE staged ({_STAGED_COLUMNS})")

    # Callbacks for `run_import` -------------------------------------------------------------

    def on_file(self, report: FileReport, path: Path) -> None:
        assert report.format is not None
        self._current = (report.index, report.format.value)
        self._paths[report.index] = path

    def sink(self, message: Message) -> None:
        assert self._current is not None
        file_index, source_format = self._current
        key = (source_format, message.conversation_id)
        owner = self._owners.setdefault(key, file_index)
        if owner != file_index:
            raise InputError(InputErrorCode.CONVERSATION_ID_CONFLICT, same_name_as_file=owner)
        seq = self._next_seq.get(key, 0)
        self._next_seq[key] = seq + 1
        self._buffer.append(
            (
                file_index,
                source_format,
                message.conversation_id,
                seq,
                format_timestamp(message.timestamp),
                message.sender,
                message.direction.value if message.direction else None,
                message.text,
                message.message_type.value,
                message.media_kind.value if message.media_kind else None,
            )
        )
        if len(self._buffer) >= STAGING_BATCH:
            self._flush()

    # Applying -----------------------------------------------------------------------------

    def apply(self, report: ImportReport, options: ImportOptions) -> ImportOutcome:
        """Write the staged conversations. Raises `ReplacementConflictError` (and writes
        nothing) when a replacement looks like a different chat."""
        self._flush()
        self._connection.execute(
            "CREATE INDEX temp.staged_order ON staged (source_format, conversation_key, seq)"
        )
        outcome = ImportOutcome()
        try:
            with transaction(self._connection):
                import_id = self._insert_import(report, options)
                sources = self._insert_sources(import_id, report)
                rows = self._connection.execute(
                    f"SELECT {_STAGED_COLUMNS} FROM staged "
                    "ORDER BY source_format, conversation_key, seq"
                )
                for _, group in groupby(
                    (_Row(*row) for row in rows),
                    key=lambda r: (r.source_format, r.conversation_key),
                ):
                    self._apply_conversation(list(group), import_id, sources, outcome)
                if outcome.conflicts:
                    raise ReplacementConflictError(
                        len(outcome.conflicts), sorted(set(outcome.conflicts))
                    )
                _raise_if_nothing_changed(outcome)
                self._connection.execute(
                    "UPDATE imports SET conversations_new = ?, conversations_changed = ?, "
                    "conversations_unchanged = ?, finished_at = ? WHERE id = ?",
                    (
                        outcome.conversations_new,
                        outcome.conversations_changed,
                        outcome.conversations_unchanged,
                        utc_now(),
                        import_id,
                    ),
                )
                outcome.import_id = import_id
        except _NothingChangedError:
            outcome.import_id = None
        finally:
            self._connection.execute("DROP TABLE IF EXISTS temp.staged")
        return outcome

    def _flush(self) -> None:
        if self._buffer:
            self._connection.executemany(
                f"INSERT INTO staged ({_STAGED_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._buffer,
            )
            self._buffer.clear()

    def _insert_import(self, report: ImportReport, options: ImportOptions) -> int:
        cursor = self._connection.execute(
            "INSERT INTO imports (started_at, finished_at, tool_version, options_json, "
            "report_json, conversations_new, conversations_changed, conversations_unchanged) "
            "VALUES (?, ?, ?, ?, ?, 0, 0, 0)",
            (
                self._started_at,
                utc_now(),
                __version__,
                canonical_json(_options_dict(options)),
                json.dumps(report.to_dict(include_file_names=False), sort_keys=True),
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def _insert_sources(self, import_id: int, report: ImportReport) -> dict[int, int]:
        sources: dict[int, int] = {}
        for file_report in report.accepted_files:
            path = self._paths.get(file_report.index)
            if path is None or file_report.format is None:
                continue
            cursor = self._connection.execute(
                "INSERT INTO sources (import_id, file_index, file_name, format, content_sha256, "
                "size_bytes, records) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    import_id,
                    file_report.index,
                    path.name,
                    file_report.format.value,
                    _file_sha256(path),
                    path.stat().st_size,
                    file_report.records_valid,
                ),
            )
            assert cursor.lastrowid is not None
            sources[file_report.index] = cursor.lastrowid
        return sources

    def _apply_conversation(
        self,
        rows: Sequence[_Row],
        import_id: int,
        sources: dict[int, int],
        outcome: ImportOutcome,
    ) -> None:
        first = rows[0]
        content = content_sha256(rows)
        existing = self._connection.execute(
            "SELECT c.id, c.current_version_id, v.content_sha256 FROM conversations c "
            "LEFT JOIN conversation_versions v ON v.id = c.current_version_id "
            "WHERE c.source_format = ? AND c.conversation_key = ?",
            (first.source_format, first.conversation_key),
        ).fetchone()

        if existing is None:
            cursor = self._connection.execute(
                "INSERT INTO conversations (source_format, conversation_key, created_import_id) "
                "VALUES (?, ?, ?)",
                (first.source_format, first.conversation_key, import_id),
            )
            assert cursor.lastrowid is not None
            conversation_id = cursor.lastrowid
            outcome.conversations_new += 1
        else:
            conversation_id = existing["id"]
            if existing["content_sha256"] == content:
                outcome.conversations_unchanged += 1
                return
            if not self._allow_unrelated and not self._overlaps(
                existing["current_version_id"], rows
            ):
                outcome.conflicts.append(first.file_index)
                return
            outcome.conversations_changed += 1

        previous = self._connection.execute(
            "SELECT id FROM conversation_versions WHERE conversation_id = ? AND content_sha256 = ?",
            (conversation_id, content),
        ).fetchone()
        if previous is not None:  # content seen before (A -> B -> A): reuse that version
            version_id = previous["id"]
            outcome.versions_reactivated += 1
        else:
            version_id = self._insert_version(
                conversation_id, content, rows, import_id, sources[first.file_index]
            )
            outcome.messages_stored += len(rows)
        self._connection.execute(
            "UPDATE conversations SET current_version_id = ? WHERE id = ?",
            (version_id, conversation_id),
        )

    def _insert_version(
        self,
        conversation_id: int,
        content: str,
        rows: Sequence[_Row],
        import_id: int,
        source_id: int,
    ) -> int:
        inbound_senders = len(
            {r.sender.strip() for r in rows if r.direction == "inbound" and r.sender.strip()}
        )
        timestamps = [r.timestamp for r in rows]
        cursor = self._connection.execute(
            "INSERT INTO conversation_versions (conversation_id, content_sha256, message_count, "
            "first_at, last_at, inbound_senders, is_group, import_id, source_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                content,
                len(rows),
                min(timestamps),
                max(timestamps),
                inbound_senders,
                int(is_group(inbound_senders)),
                import_id,
                source_id,
            ),
        )
        assert cursor.lastrowid is not None
        version_id = cursor.lastrowid
        self._connection.executemany(
            "INSERT INTO messages (version_id, seq, timestamp, sender, direction, text, "
            "message_type, media_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ((version_id, *row.stored()) for row in rows),
        )
        return version_id

    def _overlaps(self, version_id: int, rows: Iterable[_Row]) -> bool:
        keys = {row.overlap_key() for row in rows}
        stored = self._connection.execute(
            "SELECT timestamp, direction, message_type, text FROM messages WHERE version_id = ?",
            (version_id,),
        )
        return any(tuple(row) in keys for row in stored)


def _raise_if_nothing_changed(outcome: ImportOutcome) -> None:
    """Leaves the transaction by rolling back, so an unchanged import writes nothing."""
    if not (outcome.conversations_new or outcome.conversations_changed):
        raise _NothingChangedError


def is_group(inbound_senders: int) -> bool:
    """A chat is a group when more than one named person sends messages to the owner."""
    return inbound_senders >= 2


def content_sha256(rows: Iterable[_Row]) -> str:
    """Hash of a conversation's canonical messages in source order."""
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical_json(list(row.stored()[1:])).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _options_dict(options: ImportOptions) -> dict[str, object]:
    return {
        "format": options.format.value if options.format else None,
        "owner_label": options.owner_label,
        "date_order": options.date_order.value if options.date_order else None,
        "mapping": options.mapping.name if options.mapping else None,
        "column_overrides": options.column_overrides,
        "sheet": options.sheet,
        "header_row": options.header_row,
        "encoding": options.encoding,
        "delimiter": options.delimiter,
    }
