"""Prepare one conversation as model input: selection, masking, escaping, truncation, budget.

Rules (docs/llm.md):

- Current version only, canonical order (timestamp, seq); system messages and `ignored_texts`
  are left out. Group chats are excluded unless `include_groups` is set.
- Message ids (`m1`, `m2`, ...) number the eligible messages in order, so an id stays the same
  whichever subset is shown.
- Sender names are never included: messages are labeled OWNER or CONTACT.
- Text is masked (see `masking`), then `&`, `<` and `>` are escaped, so content can never close
  the `<conversation>` block. Continuation lines are indented, so content can never start a line
  that looks like a new message.
- Each message is cut at `max_message_chars` with an explicit `[truncated: N chars]` marker.
  The most recent messages that fit `max_messages` and `max_context_tokens` are kept, and an
  explicit `[N earlier messages omitted]` line replaces the rest. If not even the latest message
  fits, preparation fails instead of sending something silently cut.
- Injection heuristics only raise a security flag with the ids of the messages involved. They
  never change the content or the classification: people legitimately discuss instructions,
  prompts and code.
"""

import html
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field

from whatsapp_analyzer.config import ContextSettings
from whatsapp_analyzer.hashing import canonical_json, sha256_hex
from whatsapp_analyzer.llm.masking import MASKING_VERSION, Masker
from whatsapp_analyzer.llm.tokens import estimate_text_tokens
from whatsapp_analyzer.llm.types import SecurityFlag, SecurityReport
from whatsapp_analyzer.metrics.raw import StoredMessage
from whatsapp_analyzer.schema import Direction, MessageType

CONTINUATION_INDENT = "    "
RAW_CAP_FACTOR = 4
MAX_RAW_CHARS = 16_000
# A placeholder ("[PHONE_1") or an entity ("&amp") cut at the end of a truncated text.
_PARTIAL_TAIL = re.compile(r"(?:\[[A-Z_]*\d*|&#?\w*)$")
# Every character Python's str.splitlines() treats as a line break.
_LINE_BREAKS = re.compile(r"\r\n|[\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")

_INJECTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|your)\b"
        r".{0,20}\b(instructions?|prompts?|rules)\b",
        r"\b(ignore|ignora|esque[cç]a|desconsidere)\b.{0,40}"
        r"\b(instru[cç](?:[oõ]es|[aã]o)|regras|prompt)\b",
        r"^\s*(system|assistant|developer)\s*:",
        r"</?\s*(system|conversation|instructions?)\s*>",
        r"\byou are now\b|\bvoc[eê] agora [eé]\b",
        r"\{\s*\"level\"\s*:",
    )
)
_DELIMITER = re.compile(r"</?\s*conversation", re.IGNORECASE)


class ContextError(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind  # GROUP_EXCLUDED, NO_MESSAGES, CONTEXT_BUDGET_EXCEEDED


@dataclass(frozen=True)
class PreparedContext:
    text: str
    message_ids: frozenset[str]
    contact_ids: frozenset[str]  # ids of inbound messages among the shown ones
    shown: int
    omitted: int
    truncated: int
    estimated_tokens: int
    masking: dict[str, int]
    security: SecurityReport = field(default_factory=SecurityReport)
    context_hash: str = ""

    def __repr__(self) -> str:  # never show content
        return f"PreparedContext(shown={self.shown}, context_hash={self.context_hash[:12]!r})"

    def summary(self, settings: ContextSettings) -> dict[str, object]:
        return {
            "messages_shown": self.shown,
            "messages_omitted": self.omitted,
            "messages_truncated": self.truncated,
            "estimated_tokens": self.estimated_tokens,
            "max_context_tokens": settings.max_context_tokens,
            "masking": self.masking,
            "security_flags": sorted(f.value for f in self.security.flags),
            "suspicious_messages": len(self.security.suspicious_messages),
            "context_hash": self.context_hash,
        }


def build_context(
    messages: Sequence[StoredMessage],
    settings: ContextSettings,
    *,
    is_group: bool,
    ignored_texts: Iterable[str],
    contact_names: Iterable[str] = (),
) -> PreparedContext:
    """`contact_names`: extra names to mask as the contact (for example the conversation key,
    which often is the contact's name), besides every inbound sender of the conversation."""
    if is_group and not settings.include_groups:
        raise ContextError("GROUP_EXCLUDED")
    ignored = {text.strip() for text in ignored_texts}
    ordered = sorted(messages, key=lambda m: (m.timestamp, m.seq))
    eligible = [
        m
        for m in ordered
        if m.direction is not None
        and m.message_type is not MessageType.SYSTEM
        and not (m.message_type is MessageType.TEXT and m.text.strip() in ignored)
    ]
    if not eligible:
        raise ContextError("NO_MESSAGES")

    # Names come from every message, not only the eligible ones: a chat where the contact never
    # replied still mentions the contact's name.
    contacts = {*contact_names, *(m.sender for m in ordered if m.direction is Direction.INBOUND)}
    owners = {m.sender for m in ordered if m.direction is Direction.OUTBOUND}
    masker = Masker(contacts, owners)
    lines: list[_Line] = []
    for number, message in enumerate(eligible, start=1):
        lines.append(_line(message, f"m{number}", masker, settings.max_message_chars))

    selected = _select(lines, settings)
    omitted = len(lines) - len(selected)
    text = "\n".join(
        ([f"[{omitted} earlier messages omitted]"] if omitted else []) + [x.text for x in selected]
    )
    security = SecurityReport()
    for line in selected:
        security.flags |= line.flags
        if SecurityFlag.INJECTION_SUSPECTED in line.flags:
            security.suspicious_messages.append(line.message_id)
    context_hash = sha256_hex(
        canonical_json(
            {"masking_version": MASKING_VERSION, "settings": asdict(settings), "text": text}
        )
    )
    return PreparedContext(
        text=text,
        message_ids=frozenset(x.message_id for x in selected),
        contact_ids=frozenset(x.message_id for x in selected if x.contact),
        shown=len(selected),
        omitted=omitted,
        truncated=sum(1 for x in selected if x.truncated),
        estimated_tokens=estimate_text_tokens(text),
        masking=masker.report(),
        security=security,
        context_hash=context_hash,
    )


@dataclass(frozen=True)
class _Line:
    message_id: str
    text: str
    contact: bool
    truncated: bool
    flags: frozenset[SecurityFlag]


def _select(lines: list[_Line], settings: ContextSettings) -> list[_Line]:
    """The most recent lines within `max_messages` and the token budget, including the cost of
    the omission marker, so the block never exceeds `max_context_tokens`."""
    marker_cost = estimate_text_tokens(f"[{len(lines)} earlier messages omitted]") + 1
    selected: list[_Line] = []
    tokens = 0
    for line in reversed(lines):
        cost = estimate_text_tokens(line.text) + 1
        if len(selected) >= settings.max_messages or tokens + cost > settings.max_context_tokens:
            break
        selected.append(line)
        tokens += cost
    while (
        selected
        and len(selected) < len(lines)
        and tokens + marker_cost > settings.max_context_tokens
    ):
        tokens -= estimate_text_tokens(selected.pop().text) + 1  # drop the oldest kept line
    if not selected:
        raise ContextError("CONTEXT_BUDGET_EXCEEDED")
    selected.reverse()
    return selected


def _line(message: StoredMessage, message_id: str, masker: Masker, max_chars: int) -> _Line:
    role = "OWNER" if message.direction is Direction.OUTBOUND else "CONTACT"
    stamp = message.timestamp.strftime("%Y-%m-%d %H:%M")
    flags = _inspect(message.text)
    truncated = False
    if message.message_type is MessageType.DELETED:
        body = "[deleted message]"
    else:
        # Order matters: line breaks are unified; identifiers are masked on the raw text, as
        # sentinels that escaping cannot touch; then &, <, > and [ are escaped (content can
        # neither close the block nor imitate a marker or placeholder); then the sentinels
        # become placeholders; only then is the text cut, never inside an entity or placeholder.
        original = _LINE_BREAKS.sub("\n", unicodedata.normalize("NFC", message.text)).strip()
        # Cap the raw text first: masking cost grows with length, and anything beyond the cap
        # would be cut below anyway. The absolute ceiling bounds the cost for any setting.
        raw_cap = min(max_chars * RAW_CAP_FACTOR, MAX_RAW_CHARS)
        text = masker.mask_tokens(original[:raw_cap])
        text = masker.expand(html.escape(text, quote=False).replace("[", "&#91;"))
        if len(text) > max_chars or len(original) > raw_cap:
            kept = _PARTIAL_TAIL.sub("", text[:max_chars])
            text = f"{kept} [truncated: message had {len(original)} chars]"
            truncated = True
        if message.message_type is MessageType.MEDIA:
            kind = message.media_kind.value if message.media_kind else "file"
            body = f"[media: {kind}]" + (f" {text}" if text else "")
        else:
            body = text
    body = body.replace("\n", "\n" + CONTINUATION_INDENT)
    return _Line(
        message_id=message_id,
        text=f"[{message_id}] {stamp} {role}: {body}",
        contact=message.direction is Direction.INBOUND,
        truncated=truncated,
        flags=flags,
    )


def _inspect(text: str) -> frozenset[SecurityFlag]:
    flags = set()
    if _DELIMITER.search(text):
        flags.add(SecurityFlag.DELIMITER_IN_CONTENT)
    if any(pattern.search(text) for pattern in _INJECTION_PATTERNS):
        flags.add(SecurityFlag.INJECTION_SUSPECTED)
    return frozenset(flags)
