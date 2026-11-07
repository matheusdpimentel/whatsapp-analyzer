"""Adapters for tabular inputs: CSV (streamed) and XLSX (read-only mode)."""

import csv
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from whatsapp_analyzer.ingest.csv_export import unescape_formula
from whatsapp_analyzer.ingest.mapping import SYSTEM_DIRECTION, ColumnMapping, resolve
from whatsapp_analyzer.ingest.model import (
    InputError,
    InputErrorCode,
    IssueCode,
    Message,
    RecordIssue,
)
from whatsapp_analyzer.ingest.text_io import DELETED_MESSAGE_TEXTS, detect_encoding, open_text
from whatsapp_analyzer.schema import Direction, MediaKind, MessageType

CSV_DELIMITERS = ",;\t|"
SNIFF_CHARACTERS = 64 * 1024
MAX_FIELD_CHARACTERS = 16 * 1024 * 1024
HEADER_SEARCH_ROWS = 20

_DIRECTIONS = {d.value for d in Direction}
_MESSAGE_TYPES = {t.value for t in MessageType}
_MEDIA_KINDS = {k.value for k in MediaKind}

Cell = object
ParsedItem = Message | RecordIssue

csv.field_size_limit(MAX_FIELD_CHARACTERS)


@dataclass(frozen=True)
class TabularSettings:
    owner_label: str
    mapping: ColumnMapping | None = None
    overrides: Mapping[str, str] = field(default_factory=dict)
    encoding: str | None = None
    delimiter: str | None = None
    sheet: str | None = None
    header_row: int | None = None


def parse_csv(path: Path, settings: TabularSettings) -> Iterator[ParsedItem]:
    with open_text(path, detect_encoding(path, settings.encoding)) as handle:
        sample = handle.read(SNIFF_CHARACTERS)
        if not sample.strip():
            raise InputError(InputErrorCode.EMPTY_FILE)
        handle.seek(0)
        reader = csv.reader(handle, delimiter=settings.delimiter or _sniff_delimiter(sample))
        header = [column.strip() for column in next(reader)]
        mapping = resolve(header, settings.mapping, settings.overrides)
        # Data rows are numbered like spreadsheet rows: the header is row 1.
        for row_number, row in enumerate(reader, start=2):
            if not any(cell.strip() for cell in row):
                yield RecordIssue(IssueCode.EMPTY_ROW, row_number)
            elif len(row) != len(header):
                yield RecordIssue(IssueCode.MALFORMED_RECORD, row_number)
            else:
                cells = dict(zip(header, row, strict=True))
                yield map_row(cells, mapping, settings.owner_label, row_number)


def _sniff_delimiter(sample: str) -> str:
    first_line = sample.splitlines()[0]
    try:
        return csv.Sniffer().sniff(first_line, delimiters=CSV_DELIMITERS).delimiter
    except csv.Error:
        return ","


def parse_xlsx(path: Path, settings: TabularSettings) -> Iterator[ParsedItem]:
    workbook = _open_workbook(path)
    with closing(workbook):
        worksheet = _select_sheet(workbook, settings)
        rows = worksheet.iter_rows(values_only=True)
        header_number, header, mapping = _find_header(rows, settings)
        for row_number, row in enumerate(rows, start=header_number + 1):
            values = [*row, *[None] * (len(header) - len(row))]
            if all(value is None or str(value).strip() == "" for value in values):
                yield RecordIssue(IssueCode.EMPTY_ROW, row_number)
            elif any(value is not None for value in values[len(header) :]):
                yield RecordIssue(IssueCode.MALFORMED_RECORD, row_number)
            else:
                cells = dict(zip(header, values, strict=False))
                yield map_row(cells, mapping, settings.owner_label, row_number)


def _open_workbook(path: Path) -> Workbook:
    try:
        # data_only: read cached cell values; formulas are never evaluated.
        return load_workbook(path, read_only=True, data_only=True)
    except FileNotFoundError as exc:
        raise InputError(InputErrorCode.FILE_NOT_FOUND) from exc
    except PermissionError as exc:
        raise InputError(InputErrorCode.PERMISSION_DENIED) from exc
    except (InvalidFileException, zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise InputError(InputErrorCode.SPREADSHEET_CORRUPTED) from exc


def _select_sheet(workbook: Workbook, settings: TabularSettings) -> Worksheet:
    names: list[str] = workbook.sheetnames
    if settings.sheet is not None:
        if settings.sheet not in names:
            raise InputError(InputErrorCode.SHEET_NOT_FOUND, sheets=len(names))
        return cast(Worksheet, workbook[settings.sheet])
    if len(names) == 1:
        return cast(Worksheet, workbook[names[0]])
    matching = [name for name in names if _has_header(cast(Worksheet, workbook[name]), settings)]
    if len(matching) != 1:
        raise InputError(InputErrorCode.AMBIGUOUS_SHEET, sheets=len(names), matching=len(matching))
    return cast(Worksheet, workbook[matching[0]])


def _has_header(worksheet: Worksheet, settings: TabularSettings) -> bool:
    try:
        _find_header(worksheet.iter_rows(values_only=True), settings)
    except InputError:
        return False
    return True


def _find_header(
    rows: Iterator[tuple[Cell, ...]], settings: TabularSettings
) -> tuple[int, list[str], ColumnMapping]:
    """Use the requested header row, or the first top row that an available mapping fits."""
    last_problem: InputError | None = None
    for number, row in enumerate(rows, start=1):
        if settings.header_row is not None and number < settings.header_row:
            continue
        header = ["" if cell is None else str(cell).strip() for cell in row]
        try:
            return number, header, resolve(header, settings.mapping, settings.overrides)
        except InputError as problem:
            if settings.header_row is not None:
                raise
            last_problem = problem
        if number >= HEADER_SEARCH_ROWS:
            break
    details = {"last_problem": last_problem.code.value} if last_problem else {}
    raise InputError(InputErrorCode.HEADER_NOT_FOUND, **details)


def map_row(
    row: Mapping[str, Cell], mapping: ColumnMapping, owner_label: str, line: int
) -> ParsedItem:
    """Convert one mapped row into a message, or into the first problem found."""
    fields = _RowFields(row, mapping)
    conversation_id = fields.text("conversation_id").strip()
    if not conversation_id:
        return RecordIssue(IssueCode.MISSING_REQUIRED_FIELD, line, "conversation_id")
    timestamp_or_issue = _timestamp_field(row.get(mapping.columns["timestamp"]), mapping, line)
    if isinstance(timestamp_or_issue, RecordIssue):
        return timestamp_or_issue

    declared_type = fields.mapped("message_type")
    media_value = fields.mapped("media_kind")
    if declared_type and declared_type not in _MESSAGE_TYPES:
        return RecordIssue(IssueCode.INVALID_VALUE, line, "message_type")
    if media_value and media_value not in _MEDIA_KINDS:
        return RecordIssue(IssueCode.INVALID_VALUE, line, "media_kind")

    direction_or_issue = _direction(fields.mapped("direction"), declared_type, line)
    if isinstance(direction_or_issue, RecordIssue):
        return direction_or_issue
    direction = direction_or_issue
    is_system = direction is None

    text = fields.text("text")
    media_kind = MediaKind(media_value) if media_value else None
    if is_system:
        message_type = MessageType.SYSTEM
    elif declared_type:
        message_type = MessageType(declared_type)
    elif media_kind is not None:
        message_type = MessageType.MEDIA
    elif text.strip() in DELETED_MESSAGE_TEXTS:
        message_type, text = MessageType.DELETED, ""
    else:
        message_type = MessageType.TEXT
    sender = fields.text("sender").strip()
    if not sender and direction is Direction.OUTBOUND:
        sender = owner_label
    return Message(
        conversation_id, timestamp_or_issue, sender, direction, text, message_type, media_kind, line
    )


def _direction(value: str, declared_type: str, line: int) -> Direction | RecordIssue | None:
    """`None` means a system message: direction "system", or empty with message_type "system"."""
    if value == SYSTEM_DIRECTION or (not value and declared_type == MessageType.SYSTEM.value):
        return None
    if not value:
        return RecordIssue(IssueCode.MISSING_REQUIRED_FIELD, line, "direction")
    if value not in _DIRECTIONS:
        return RecordIssue(IssueCode.INVALID_DIRECTION, line, "direction")
    return Direction(value)


class _RowFields:
    """Reads canonical fields from a source row through a mapping."""

    def __init__(self, row: Mapping[str, Cell], mapping: ColumnMapping) -> None:
        self._row = row
        self._mapping = mapping

    def text(self, name: str) -> str:
        column = self._mapping.columns.get(name)
        value = self._row.get(column) if column else None
        if isinstance(value, bool):  # spreadsheet booleans read like the CSV spelling
            return "true" if value else "false"
        text = "" if value is None else str(value)
        if self._mapping.unescape_formulas and name in ("conversation_id", "sender", "text"):
            text = unescape_formula(text)
        return text

    def mapped(self, name: str) -> str:
        raw = self.text(name).strip()
        return self._mapping.values.get(name, {}).get(raw, raw)


def _timestamp_field(value: Cell, mapping: ColumnMapping, line: int) -> datetime | RecordIssue:
    if value is None or (isinstance(value, str) and not value.strip()):
        return RecordIssue(IssueCode.MISSING_REQUIRED_FIELD, line, "timestamp")
    parsed = _timestamp(value, mapping.timestamp_format)
    return (
        parsed
        if parsed is not None
        else RecordIssue(IssueCode.INVALID_TIMESTAMP, line, "timestamp")
    )


def _timestamp(value: Cell, timestamp_format: str | None) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            parsed = (
                datetime.strptime(text, timestamp_format)
                if timestamp_format
                else datetime.fromisoformat(text)
            )
        except ValueError:
            return None
    else:
        return None
    # The canonical schema stores naive local times; offsets are never converted silently.
    return parsed if parsed.tzinfo is None else None
