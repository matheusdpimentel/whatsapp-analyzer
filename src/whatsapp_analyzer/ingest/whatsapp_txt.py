"""Adapter for chat exports written by the WhatsApp app itself ("Export chat").

Experimental: the layouts follow `export_formats` (Android `d/m/y, h:mm - Sender: text` and iOS
`[d/m/y, h:mm:ss] Sender: text`) but have not been validated against a broad set of real exports.

Native exports do not say which messages are outgoing, so the owner's display name is required.
They also do not say whether dates are day-first or month-first; the order is inferred from the
whole file (a day above 12 settles it) and the file is rejected when it cannot be inferred and
`date_order` was not given. Nothing is guessed silently.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from whatsapp_analyzer.export_formats import EXPORT_FORMATS
from whatsapp_analyzer.ingest.model import (
    InputError,
    InputErrorCode,
    IssueCode,
    Message,
    RecordIssue,
)
from whatsapp_analyzer.ingest.text_io import DELETED_MESSAGE_TEXTS, iter_lines, open_text
from whatsapp_analyzer.schema import Direction, MediaKind, MessageType

LRM = "\u200e"
_DATE = r"(?P<date>\d{1,4}(?P<sep>[./-])\d{1,2}(?P=sep)\d{1,4})"
_TIME = r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?)(?:[ \u202f\u00a0](?P<ampm>[AaPp]\.? ?[Mm]\.?))?"
ANDROID_HEADER = re.compile(rf"^{_DATE},? {_TIME} - (?P<rest>.*)$")
IOS_HEADER = re.compile(rf"^{LRM}?\[{_DATE},? {_TIME}\] (?P<rest>.*)$")
ATTACHMENT = re.compile(r"^<(?:attached|anexado): (?P<name>[^>]+)>$")
ATTACHMENT_TOKENS = {
    "PHOTO": MediaKind.IMAGE,
    "VIDEO": MediaKind.VIDEO,
    "GIF": MediaKind.VIDEO,
    "AUDIO": MediaKind.AUDIO,
    "STICKER": MediaKind.STICKER,
}
MEDIA_OMITTED = frozenset(fmt.media_omitted for fmt in EXPORT_FORMATS.values() if fmt.media_omitted)


class DateOrder(StrEnum):
    DAY_FIRST = "dmy"
    MONTH_FIRST = "mdy"
    YEAR_FIRST = "ymd"


def looks_like(first_lines: list[str]) -> bool:
    content = [line for line in first_lines if line.strip()][:10]
    return any(_match_header(line) for line in content)


def parse(
    path: Path, encoding: str, owner: str | None, date_order: DateOrder | None
) -> Iterator[Message | RecordIssue]:
    if not owner:
        raise InputError(InputErrorCode.OWNER_REQUIRED)
    order, senders = _scan(path, encoding)
    order = date_order or order
    if order is None:
        raise InputError(InputErrorCode.AMBIGUOUS_DATE_ORDER)
    if owner not in senders:
        # Legitimate when the owner never replied, but also the symptom of a misspelled --owner,
        # in which case every message would look received. Reported, never silently assumed.
        yield RecordIssue(IssueCode.OWNER_NOT_IN_FILE, 0)

    conversation_id = path.stem
    current: _Pending | None = None
    reported_preamble = False
    with open_text(path, encoding) as handle:
        for number, line in enumerate(iter_lines(handle), start=1):
            match = _match_header(line)
            if match is None:
                if current is not None:
                    current.lines.append(line)
                elif line.strip() and not reported_preamble:
                    reported_preamble = True
                    yield RecordIssue(IssueCode.MALFORMED_RECORD, number)
                continue
            if current is not None:
                yield _finish(current, owner)
            timestamp = _timestamp(match, order)
            if timestamp is None:
                current = None
                yield RecordIssue(IssueCode.INVALID_TIMESTAMP, number, "timestamp")
                continue
            current = _Pending(
                conversation_id, number, timestamp, ios=line.lstrip(LRM).startswith("[")
            )
            current.lines.append(match.group("rest"))
    if current is not None:
        yield _finish(current, owner)


@dataclass
class _Pending:
    conversation_id: str
    line: int
    timestamp: datetime
    ios: bool
    lines: list[str] = field(default_factory=list)


def _match_header(line: str) -> re.Match[str] | None:
    return ANDROID_HEADER.match(line) or IOS_HEADER.match(line)


def _scan(path: Path, encoding: str) -> tuple[DateOrder | None, set[str]]:
    """First pass: infer the date order and collect sender names (kept in memory only)."""
    day_first = month_first = year_first = False
    senders: set[str] = set()
    with open_text(path, encoding) as handle:
        for line in iter_lines(handle):
            match = _match_header(line)
            if match is None:
                continue
            first, second, _ = match.group("date").split(match.group("sep"))
            if len(first) == 4:
                year_first = True
            elif int(first) > 12:
                day_first = True
            elif int(second) > 12:
                month_first = True
            sender, separator, _ = match.group("rest").partition(": ")
            if separator:
                senders.add(sender)
    if sum((day_first, month_first, year_first)) > 1:
        raise InputError(InputErrorCode.MIXED_DATE_FORMATS)
    if year_first:
        return DateOrder.YEAR_FIRST, senders
    if day_first:
        return DateOrder.DAY_FIRST, senders
    if month_first:
        return DateOrder.MONTH_FIRST, senders
    return None, senders


def _timestamp(match: re.Match[str], order: DateOrder) -> datetime | None:
    parts = [int(part) for part in match.group("date").split(match.group("sep"))]
    if order is DateOrder.YEAR_FIRST:
        year, month, day = parts
    elif order is DateOrder.DAY_FIRST:
        day, month, year = parts
    else:
        month, day, year = parts
    if year < 100:
        year += 2000
    hour, minute, *rest = (int(part) for part in match.group("time").split(":"))
    second = rest[0] if rest else 0
    ampm = (match.group("ampm") or "").lower()
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm.startswith("p") else 0)
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def _finish(pending: _Pending, owner: str) -> Message:
    first, *continuation = pending.lines
    sender, separator, body = first.partition(": ")
    if not separator:
        return _message(pending, "", None, [first, *continuation], MessageType.SYSTEM)
    marker = body.lstrip(LRM)
    attachment = ATTACHMENT.match(marker)
    deleted = marker in DELETED_MESSAGE_TEXTS and not continuation
    if pending.ios and body.startswith(LRM) and not (attachment or deleted):
        # iOS attributes notices to the chat title and marks them with a left-to-right mark.
        return _message(pending, "", None, [marker, *continuation], MessageType.SYSTEM)

    direction = Direction.OUTBOUND if sender == owner else Direction.INBOUND
    if attachment:
        kind = _attachment_kind(attachment.group("name"))
        return _message(pending, sender, direction, continuation, MessageType.MEDIA, kind)
    if marker in MEDIA_OMITTED:
        # Android drops the file and its kind; caption lines, if any, follow the marker.
        return _message(pending, sender, direction, continuation, MessageType.MEDIA)
    if deleted:
        return _message(pending, sender, direction, [], MessageType.DELETED)
    return _message(pending, sender, direction, [body, *continuation], MessageType.TEXT)


def _message(
    pending: _Pending,
    sender: str,
    direction: Direction | None,
    lines: list[str],
    message_type: MessageType,
    media_kind: MediaKind | None = None,
) -> Message:
    text = "\n".join(lines).rstrip("\n")  # blank lines before the next header are not content
    return Message(
        pending.conversation_id,
        pending.timestamp,
        sender,
        direction,
        text,
        message_type,
        media_kind,
        pending.line,
    )


def _attachment_kind(name: str) -> MediaKind:
    for token, kind in ATTACHMENT_TOKENS.items():
        if f"-{token}-" in name:
            return kind
    return MediaKind.DOCUMENT
