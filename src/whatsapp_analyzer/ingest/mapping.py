"""Column mappings from tabular inputs (CSV, XLSX) to the canonical schema.

A mapping is never guessed from similar-looking column names. It comes from a built-in preset
whose columns all exist in the file, from a TOML mapping file, or from explicit `field=column`
overrides; anything else stops the import with the list of unmapped required fields.
"""

import tomllib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from whatsapp_analyzer.ingest.model import InputError, InputErrorCode
from whatsapp_analyzer.schema import CANONICAL_COLUMNS, Direction, MediaKind, MessageType

REQUIRED_FIELDS = ("conversation_id", "timestamp", "direction")
SYSTEM_DIRECTION = "system"  # direction value that marks a system message
ALLOWED_VALUES = {
    "direction": {d.value for d in Direction} | {SYSTEM_DIRECTION},
    "message_type": {t.value for t in MessageType},
    "media_kind": {k.value for k in MediaKind},
}


@dataclass(frozen=True)
class ColumnMapping:
    name: str
    columns: dict[str, str]  # canonical field -> source column
    timestamp_format: str | None = None  # strptime format; None means ISO 8601
    values: dict[str, dict[str, str]] = field(default_factory=dict)  # field -> source -> canonical
    unescape_formulas: bool = False  # values written by this tool's normalized CSV export

    def missing_required(self) -> list[str]:
        return [name for name in REQUIRED_FIELDS if name not in self.columns]

    def unknown_columns(self, header: Sequence[str]) -> list[str]:
        available = set(header)
        return sorted({column for column in self.columns.values() if column not in available})


CANONICAL = ColumnMapping(
    name="canonical",
    columns={column: column for column in CANONICAL_COLUMNS},
    unescape_formulas=True,
)

IMAZING_CSV = ColumnMapping(
    name="imazing-csv",
    columns={
        "conversation_id": "Chat Session",
        "timestamp": "Message Date",
        "sender": "Sender Name",
        "direction": "Type",
        "text": "Text",
        "media_kind": "Attachment type",
    },
    values={
        "direction": {"Incoming": "inbound", "Outgoing": "outbound", "Notification": "system"},
        "media_kind": {
            "Audio": "audio",
            "Image": "image",
            "Video": "video",
            "Gif": "video",
            "Sticker": "sticker",
            "Attachment": "document",
            "Contact": "contact",
            "Location": "location",
        },
    },
)

PRESETS = {preset.name: preset for preset in (CANONICAL, IMAZING_CSV)}


def detect_preset(header: Sequence[str]) -> ColumnMapping | None:
    available = set(header)
    for preset in PRESETS.values():
        if set(preset.columns.values()) <= available:
            return preset
    return None


def resolve(
    header: Sequence[str],
    base: ColumnMapping | None,
    overrides: Mapping[str, str],
) -> ColumnMapping:
    """Choose the mapping for `header` and check it, raising `InputError` when it is unusable."""
    mapping = base or detect_preset(header) or ColumnMapping("custom", {})
    if overrides:
        mapping = replace(
            mapping, name=f"{mapping.name}+overrides", columns={**mapping.columns, **overrides}
        )
    if missing := mapping.missing_required():
        raise InputError(InputErrorCode.MISSING_REQUIRED_COLUMNS, fields=missing)
    if unknown := mapping.unknown_columns(header):
        raise InputError(InputErrorCode.INVALID_COLUMN_MAPPING, columns_not_found=unknown)
    duplicated = [name for name, count in Counter(header).items() if count > 1]
    if ambiguous := sorted(set(duplicated) & set(mapping.columns.values())):
        raise InputError(
            InputErrorCode.INVALID_COLUMN_MAPPING, columns_repeated_in_header=ambiguous
        )
    return mapping


def parse_overrides(pairs: Sequence[str]) -> dict[str, str]:
    """Parse `field=column` pairs given on the command line."""
    overrides: dict[str, str] = {}
    for pair in pairs:
        name, separator, column = pair.partition("=")
        name, column = name.strip(), column.strip()
        if not separator or not column or name not in CANONICAL_COLUMNS:
            raise InputError(InputErrorCode.INVALID_COLUMN_MAPPING, override=pair)
        overrides[name] = column
    return overrides


def load_mapping_file(path: Path) -> ColumnMapping:
    """Load a mapping file::

    [columns]
    conversation_id = "chat_id"
    timestamp = "sent_at"
    direction = "from_me"

    [timestamp]
    format = "%d/%m/%Y %H:%M:%S"   # optional; ISO 8601 when omitted

    [values.direction]
    "true" = "outbound"
    "false" = "inbound"
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InputError(InputErrorCode.FILE_NOT_FOUND, mapping_file=True) from exc
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise InputError(InputErrorCode.INVALID_COLUMN_MAPPING, mapping_file=True) from exc

    columns = data.get("columns", {})
    values = data.get("values", {})
    timestamp_table = data.get("timestamp", {})
    timestamp_format = timestamp_table.get("format") if isinstance(timestamp_table, dict) else None
    problems: list[str] = []
    if not isinstance(columns, dict) or not all(isinstance(v, str) for v in columns.values()):
        problems.append("columns")
        columns = {}
    problems += [f"columns.{name}" for name in columns if name not in CANONICAL_COLUMNS]
    if not isinstance(timestamp_table, dict):
        problems.append("timestamp")
    elif timestamp_format is not None and not isinstance(timestamp_format, str):
        problems.append("timestamp.format")
    if not isinstance(values, dict):
        problems.append("values")
        values = {}
    for name, table in values.items():
        if name not in ALLOWED_VALUES or not isinstance(table, dict):
            problems.append(f"values.{name}")
            continue
        problems += [
            f"values.{name}.{source}"
            for source, target in table.items()
            if target not in ALLOWED_VALUES[name]
        ]
    if problems:
        raise InputError(
            InputErrorCode.INVALID_COLUMN_MAPPING, mapping_file=True, invalid_keys=problems
        )
    return ColumnMapping(
        name="file",
        columns=dict(columns),
        timestamp_format=timestamp_format,
        values={name: dict(table) for name, table in values.items()},
    )
