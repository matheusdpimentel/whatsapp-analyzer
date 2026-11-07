"""Canonical message schema shared by every input format.

TXT exports are parsed into this shape, and CSV/XLSX inputs are mapped onto it. Timestamps are
naive local times, as WhatsApp writes them, serialized as ISO 8601 (`2026-01-31T14:05:09`).
Whether a message contains a link is derived later from its text, so it is not a message type.

Required fields are `conversation_id`, `timestamp` and `direction` (empty only for system
messages). `sender` may be empty when an exporter does not name the sender; outbound messages
get the owner label. Reply quotes, reactions and delivery status are not part of the schema.
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
    CONTACT = "contact"  # a shared contact card
    LOCATION = "location"  # a shared location


# Kinds that correspond to an attached file; contacts and locations are not files.
FILE_MEDIA_KINDS = (
    MediaKind.AUDIO,
    MediaKind.IMAGE,
    MediaKind.VIDEO,
    MediaKind.STICKER,
    MediaKind.DOCUMENT,
)


CANONICAL_COLUMNS = (
    "conversation_id",
    "timestamp",
    "sender",
    "direction",
    "text",
    "message_type",
    "media_kind",
)
