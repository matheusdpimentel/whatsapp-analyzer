"""Canonical message schema shared by every input format.

TXT exports are parsed into this shape, and CSV/XLSX inputs are mapped onto it. Timestamps are
naive local times, as WhatsApp writes them, serialized as ISO 8601 (`2026-01-31T14:05:09`).
Whether a message contains a link is derived later from its text, so it is not a message type.
"""

from enum import StrEnum


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageType(StrEnum):
    TEXT = "text"
    MEDIA = "media"
    DELETED = "deleted"
    SYSTEM = "system"  # has no sender and no direction


class MediaKind(StrEnum):
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"
    STICKER = "sticker"
    DOCUMENT = "document"


CANONICAL_COLUMNS = (
    "conversation_id",
    "timestamp",
    "sender",
    "direction",
    "text",
    "message_type",
    "media_kind",
)
