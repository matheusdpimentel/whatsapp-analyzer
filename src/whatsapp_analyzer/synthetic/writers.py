"""Write synthetic conversations as WhatsApp TXT exports, CSV, XLSX and a manifest.

TXT and CSV output is byte-for-byte reproducible for a given seed. XLSX files are zip archives
with embedded timestamps, so only their cell contents are reproducible.
"""

import csv
import io
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

from whatsapp_analyzer.export_formats import ExportFormat, Platform, formats_for_locale
from whatsapp_analyzer.schema import CANONICAL_COLUMNS, Direction, MessageType
from whatsapp_analyzer.synthetic.generator import (
    GENERATOR_VERSION,
    GeneratorSettings,
    SyntheticConversation,
    SyntheticMessage,
    generate,
)

XLSX_TITLE = "Synthetic WhatsApp data - not real conversations"
XLSX_SHEET = "Messages"
XLSX_HEADER_ROW = 3

# Column names and values of a CSV produced by some other tool; used to exercise column mapping.
CUSTOM_COLUMNS = ("chat_id", "sent_at", "author", "from_me", "body", "kind")
CUSTOM_TIMESTAMP_FORMAT = "%d/%m/%Y %H:%M:%S"

Row = dict[str, str]


@dataclass(frozen=True)
class DatasetSummary:
    output_dir: Path
    files: list[str]
    conversations: int
    messages: int


def render_txt(conversation: SyntheticConversation, fmt: ExportFormat) -> str:
    first = conversation.messages[0].timestamp
    lines = [fmt.system_line(first, conversation.title, fmt.encryption_notice)]
    media_sequence = 0
    for message in conversation.messages:
        if message.message_type is MessageType.MEDIA:
            media_sequence += 1
        lines.append(
            fmt.message_line(message.timestamp, message.sender, _body(fmt, message, media_sequence))
        )
    return "\n".join(lines) + "\n"


def _body(fmt: ExportFormat, message: SyntheticMessage, media_sequence: int) -> str:
    if message.message_type is MessageType.DELETED:
        return fmt.deleted_marker()
    if message.message_type is MessageType.MEDIA:
        assert message.media_kind is not None
        marker = fmt.media_marker(message.media_kind, media_sequence, message.timestamp)
        return f"{marker}\n{message.text}" if message.text else marker
    return message.text or ""


def expected_txt_records(conversation: SyntheticConversation, fmt: ExportFormat) -> list[Row]:
    """The canonical rows a parser can recover from `render_txt(conversation, fmt)`.

    Android exports drop seconds and the kind of omitted media, so those fields are lost.
    """
    first = conversation.messages[0].timestamp
    rows = [
        {
            "conversation_id": conversation.conversation_id,
            "timestamp": _iso(first, fmt.has_seconds),
            "sender": "",
            "direction": "",
            "text": fmt.encryption_notice,
            "message_type": MessageType.SYSTEM.value,
            "media_kind": "",
        }
    ]
    for message in conversation.messages:
        row = _canonical_row(conversation, message)
        row["timestamp"] = _iso(message.timestamp, fmt.has_seconds)
        if fmt.platform is Platform.ANDROID:
            row["media_kind"] = ""
        rows.append(row)
    return rows


def canonical_rows(conversations: Iterable[SyntheticConversation]) -> list[Row]:
    return [
        _canonical_row(conversation, message)
        for conversation in conversations
        for message in conversation.messages
    ]


def _canonical_row(conversation: SyntheticConversation, message: SyntheticMessage) -> Row:
    return {
        "conversation_id": conversation.conversation_id,
        "timestamp": _iso(message.timestamp, has_seconds=True),
        "sender": message.sender,
        "direction": message.direction.value,
        "text": message.text or "",
        "message_type": message.message_type.value,
        "media_kind": message.media_kind.value if message.media_kind else "",
    }


def _iso(timestamp: datetime, has_seconds: bool) -> str:
    return (timestamp if has_seconds else timestamp.replace(second=0)).isoformat()


def render_csv(conversations: Sequence[SyntheticConversation]) -> str:
    return _csv_text(
        CANONICAL_COLUMNS, (list(row.values()) for row in canonical_rows(conversations))
    )


def render_custom_csv(conversations: Sequence[SyntheticConversation]) -> str:
    def custom(conversation: SyntheticConversation, message: SyntheticMessage) -> list[str]:
        return [
            conversation.conversation_id,
            message.timestamp.strftime(CUSTOM_TIMESTAMP_FORMAT),
            message.sender,
            "true" if message.direction is Direction.OUTBOUND else "false",
            message.text or "",
            message.message_type.value,
        ]

    rows = (custom(c, m) for c in conversations for m in c.messages)
    return _csv_text(CUSTOM_COLUMNS, rows)


def _csv_text(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def write_xlsx(
    conversations: Sequence[SyntheticConversation], path: Path, created: datetime
) -> None:
    """Write the canonical rows below a title row, so header detection has something to skip."""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = XLSX_SHEET
    sheet.append([XLSX_TITLE])
    sheet.append([])
    sheet.append(list(CANONICAL_COLUMNS))
    for row in canonical_rows(conversations):
        values: list[object] = list(row.values())
        values[1] = datetime.fromisoformat(row["timestamp"])
        sheet.append(values)
    workbook.properties.creator = "whatsapp-conversation-analyzer"
    workbook.properties.created = created
    workbook.properties.modified = created
    workbook.save(path)


def render_manifest(
    settings: GeneratorSettings,
    conversations: Sequence[SyntheticConversation],
    files: Sequence[str],
) -> str:
    manifest = {
        "generator_version": GENERATOR_VERSION,
        "synthetic": True,
        "seed": settings.seed,
        "locale": settings.locale,
        "end": settings.end.isoformat(),
        "conversations": [
            {
                "id": c.conversation_id,
                "title": c.title,
                "profile": c.profile,
                "participants": len(c.participants),
                "messages": len(c.messages),
            }
            for c in conversations
        ],
        "files": list(files),
    }
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def remove_previous_dataset(output_dir: Path) -> None:
    """Delete the files listed in an existing manifest, so a rewrite leaves no stale output.

    Only files the generator wrote are removed; anything else in the directory is kept.
    """
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        listed = json.loads(manifest_path.read_text(encoding="utf-8"))["files"]
    except (ValueError, KeyError, TypeError):  # not a manifest this generator wrote
        return
    root = output_dir.resolve()
    for relative in listed if isinstance(listed, list) else []:
        path = (root / str(relative)).resolve()
        if path.is_relative_to(root) and path.is_file():
            path.unlink()
            _remove_empty_parents(path.parent, root)


def _remove_empty_parents(directory: Path, root: Path) -> None:
    while directory != root and directory.is_relative_to(root) and not any(directory.iterdir()):
        directory.rmdir()
        directory = directory.parent


def write_dataset(
    settings: GeneratorSettings,
    output_dir: Path,
    platforms: Sequence[Platform] = tuple(Platform),
) -> DatasetSummary:
    conversations = generate(settings)
    remove_previous_dataset(output_dir)
    texts: dict[str, str] = {}
    for fmt in formats_for_locale(settings.locale):
        if fmt.platform in platforms:
            for conversation in conversations:
                texts[f"txt/{fmt.id}/{conversation.conversation_id}.txt"] = render_txt(
                    conversation, fmt
                )
    texts["conversations.csv"] = render_csv(conversations)
    texts["conversations_custom_columns.csv"] = render_custom_csv(conversations)
    files = sorted([*texts, "conversations.xlsx", "manifest.json"])
    texts["manifest.json"] = render_manifest(settings, conversations, files)

    for relative, content in texts.items():
        path = output_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    write_xlsx(conversations, output_dir / "conversations.xlsx", created=settings.end)

    return DatasetSummary(
        output_dir=output_dir,
        files=files,
        conversations=len(conversations),
        messages=sum(len(c.messages) for c in conversations),
    )
