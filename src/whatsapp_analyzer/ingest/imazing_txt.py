"""Adapter for chat TXT files written by the iMazing desktop application.

Layout, as observed in real files (one chat per file)::

    ----------------------------------------------------   (exactly 52 dashes)
    <chat title>
    2024-03-01 10:00:00 from <sender> (<phone>)             (or "to <chat title>" when sent)

    [↩ <reply quote, possibly several lines>
    ]
    <text lines, attachment lines such as "x.opus (Audio)", "00:12", "120 KB", "3 pages · ...">

    [<reaction lines ending in "dd/mm/yy, hh:mm">]

A dash line only starts a record when it is followed by a title and a valid header, so dashes
typed inside a message stay part of that message. Reply quotes and reactions are not part of
the canonical schema; they are removed from the text and counted in the report.
"""

import re
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from whatsapp_analyzer.ingest.model import IssueCode, Message, RecordIssue
from whatsapp_analyzer.ingest.text_io import DELETED_MESSAGE_TEXTS, iter_lines, open_text
from whatsapp_analyzer.schema import Direction, MediaKind, MessageType

SEPARATOR = "-" * 52
HEADER = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (from|to) (.+)$")
PHONE_SUFFIX = re.compile(r"^(.+) \((\+?[\d\s().-]{6,})\)$")
REPLY_QUOTE = re.compile(r"^↩ .*, \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}: ")
# "<who> <emoji> <word> dd/mm/yy, hh:mm". The emoji token must contain a pictographic
# character (skin tones, variation selectors and joiners may follow), so punctuation such as
# "-" or "!" in an ordinary sentence never makes it look like a reaction.
_EMOJI = r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]"
_EMOJI_TOKEN = rf"{_EMOJI}[{_EMOJI[1:-1]}\U0001F3FB-\U0001F3FF\uFE0F\u200D]*"
REACTION = re.compile(rf"^\S.* {_EMOJI_TOKEN} \w+ \d{{2}}/\d{{2}}/\d{{2}}, \d{{2}}:\d{{2}}$")
ATTACHMENT = re.compile(r"\.[A-Za-z0-9]{1,6} \((Audio|Image|Sticker|Video|Gif)\)$")
DOCUMENT_NAME = re.compile(r"^\S.*\.[A-Za-z0-9]{1,6}$")
PAGE_COUNT = re.compile(r"^\d+ pages? ·")
FILE_SIZE = re.compile(r"^\d+(?:[.,]\d+)? ?(?:bytes|KB|MB|GB)$")
DURATION = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")

ATTACHMENT_KINDS = {
    "Audio": MediaKind.AUDIO,
    "Image": MediaKind.IMAGE,
    "Sticker": MediaKind.STICKER,
    "Video": MediaKind.VIDEO,
    "Gif": MediaKind.VIDEO,  # WhatsApp sends GIFs as short videos
}

ParsedItem = Message | RecordIssue


def looks_like(first_lines: list[str]) -> bool:
    """True when the start of a file has the iMazing record layout."""
    content = [line for line in first_lines if line.strip()]
    return (
        len(content) >= 3
        and content[0] == SEPARATOR
        and HEADER.match(content[2].strip()) is not None
    )


@dataclass
class _Record:
    line: int
    title: str
    header: re.Match[str]
    body: list[str] = field(default_factory=list)


def parse(path: Path, encoding: str, owner_label: str) -> Iterator[ParsedItem]:
    conversation_id = path.stem
    with open_text(path, encoding) as handle:
        lines = _Lookahead(enumerate(iter_lines(handle), start=1))
        record: _Record | None = None
        title: str | None = None
        reported_preamble = False
        for number, line in lines:
            following = lines.peek(2) if line == SEPARATOR else []
            if line == SEPARATOR and not following:
                continue  # a separator at the very end of the file closes nothing
            if following and _starts_record(following):
                if record is not None:
                    yield from _emit(record, conversation_id, owner_label)
                next(lines)
                header_number, header = next(lines)
                new_title = following[0][1].strip()
                if title is not None and new_title != title:
                    yield RecordIssue(IssueCode.TITLE_CHANGED, header_number)
                title = new_title
                match = HEADER.match(header.strip())
                assert match is not None  # checked by _starts_record
                record = _Record(header_number, new_title, match)
            elif record is not None:
                record.body.append(line)
            elif line.strip() and not reported_preamble:
                reported_preamble = True
                yield RecordIssue(IssueCode.MALFORMED_RECORD, number)
        if record is not None:
            yield from _emit(record, conversation_id, owner_label)


def _starts_record(following: list[tuple[int, str]]) -> bool:
    return (
        len(following) == 2
        and bool(following[0][1].strip())
        and HEADER.match(following[1][1].strip()) is not None
    )


def _emit(record: _Record, conversation_id: str, owner_label: str) -> Iterator[ParsedItem]:
    stamp, verb, party = record.header.groups()
    try:
        timestamp = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        yield RecordIssue(IssueCode.INVALID_TIMESTAMP, record.line, "timestamp")
        return

    lines = _trim(record.body)
    lines, had_reactions = _split_reactions(lines)
    lines, had_quote = _split_quote(lines)
    if had_reactions:
        yield RecordIssue(IssueCode.REACTIONS_REMOVED, record.line)
    if had_quote:
        yield RecordIssue(IssueCode.REPLY_QUOTE_REMOVED, record.line)
    text_lines, media_kind = _split_attachment_lines(lines)
    text = "\n".join(_trim(text_lines))

    if verb == "from":
        direction = Direction.INBOUND
        phone_match = PHONE_SUFFIX.match(party)
        sender = phone_match.group(1) if phone_match else party
    else:
        direction = Direction.OUTBOUND
        sender = owner_label

    if media_kind is not None:
        message_type = MessageType.MEDIA
    elif text.strip() in DELETED_MESSAGE_TEXTS:
        message_type, text = MessageType.DELETED, ""
    else:
        message_type = MessageType.TEXT
        if not text:
            yield RecordIssue(IssueCode.EMPTY_MESSAGE, record.line)
    yield Message(
        conversation_id=conversation_id,
        timestamp=timestamp,
        sender=sender.strip(),
        direction=direction,
        text=text,
        message_type=message_type,
        media_kind=media_kind,
        line=record.line,
    )


def _trim(lines: list[str]) -> list[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _split_reactions(lines: list[str]) -> tuple[list[str], bool]:
    """Remove a trailing block of reaction lines that follows a blank line."""
    for index in range(len(lines) - 1, -1, -1):
        if not lines[index].strip():
            tail = lines[index + 1 :]
            if tail and all(REACTION.match(line.strip()) for line in tail):
                return _trim(lines[:index]), True
            break
    return lines, False


def _split_quote(lines: list[str]) -> tuple[list[str], bool]:
    """Remove a leading reply quote, which runs until the first blank line.

    Without a blank line the end of the quote is unknown, so nothing is removed.
    """
    if not lines or not REPLY_QUOTE.match(lines[0]):
        return lines, False
    blank = next((i for i, line in enumerate(lines) if not line.strip()), None)
    if blank is None:
        return lines, False
    return _trim(lines[blank + 1 :]), True


def _split_attachment_lines(lines: list[str]) -> tuple[list[str], MediaKind | None]:
    """Separate attachment descriptions from the message text (which may be a caption)."""
    stripped = [line.strip() for line in lines]
    dropped: set[int] = set()
    kind: MediaKind | None = None
    for index, line in enumerate(stripped):
        follows_attachment = index - 1 in dropped
        if match := ATTACHMENT.search(line):
            kind = kind or ATTACHMENT_KINDS[match.group(1)]
            dropped.add(index)
        elif follows_attachment and (
            DURATION.match(line) or FILE_SIZE.match(line) or PAGE_COUNT.match(line)
        ):
            dropped.add(index)
        # A file name followed by "N pages ·" is a document. A name followed only by a size is
        # not enough: "see report.pdf" + "2 MB" typed by a person would look identical.
        elif (
            kind is None
            and index > 0
            and PAGE_COUNT.match(line)
            and DOCUMENT_NAME.match(stripped[index - 1])
        ):
            kind = MediaKind.DOCUMENT
            dropped.update({index - 1, index})
    return [line for i, line in enumerate(lines) if i not in dropped], kind


class _Lookahead:
    """Iterator wrapper that can peek at upcoming items without consuming them."""

    def __init__(self, items: Iterable[tuple[int, str]]) -> None:
        self._items = iter(items)
        self._buffer: deque[tuple[int, str]] = deque()

    def __iter__(self) -> "_Lookahead":
        return self

    def __next__(self) -> tuple[int, str]:
        return self._buffer.popleft() if self._buffer else next(self._items)

    def peek(self, count: int) -> list[tuple[int, str]]:
        while len(self._buffer) < count:
            try:
                self._buffer.append(next(self._items))
            except StopIteration:
                break
        return list(self._buffer)[:count]
