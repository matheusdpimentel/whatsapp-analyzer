"""WhatsApp chat export formats.

WhatsApp writes exports in a layout that depends on the platform, the app version and the phone's
language. The variants below model commonly observed exports; they are not an exhaustive or
official specification. The synthetic generator renders them, and the TXT parser recognizes them.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from whatsapp_analyzer.schema import MediaKind

LEFT_TO_RIGHT_MARK = "\u200e"
NARROW_NO_BREAK_SPACE = "\u202f"


class Platform(StrEnum):
    ANDROID = "android"
    IOS = "ios"


@dataclass(frozen=True)
class ExportFormat:
    id: str
    platform: Platform
    locale: str
    day_first: bool
    four_digit_year: bool
    twelve_hour_clock: bool
    has_seconds: bool
    date_time_separator: str
    media_omitted: str  # Android: media files are replaced by this marker
    attachment: str  # iOS: "{name}" is the attached file name
    deleted: str
    encryption_notice: str

    def format_timestamp(self, timestamp: datetime) -> str:
        year = f"{timestamp.year:04d}" if self.four_digit_year else f"{timestamp.year % 100:02d}"
        if self.day_first:
            date = f"{timestamp.day:02d}/{timestamp.month:02d}/{year}"
        else:
            date = f"{timestamp.month}/{timestamp.day}/{year}"
        seconds = f":{timestamp.second:02d}" if self.has_seconds else ""
        if self.twelve_hour_clock:
            hour = timestamp.hour % 12 or 12
            suffix = "AM" if timestamp.hour < 12 else "PM"
            time = f"{hour}:{timestamp.minute:02d}{seconds}{NARROW_NO_BREAK_SPACE}{suffix}"
        else:
            time = f"{timestamp.hour:02d}:{timestamp.minute:02d}{seconds}"
        return f"{date}{self.date_time_separator}{time}"

    def message_line(self, timestamp: datetime, sender: str, body: str) -> str:
        stamp = self.format_timestamp(timestamp)
        if self.platform is Platform.IOS:
            return f"[{stamp}] {sender}: {body}"
        return f"{stamp} - {sender}: {body}"

    def system_line(self, timestamp: datetime, chat_title: str, body: str) -> str:
        stamp = self.format_timestamp(timestamp)
        if self.platform is Platform.IOS:
            # iOS attributes system notices to the chat itself and marks them with an LRM.
            return f"[{stamp}] {chat_title}: {LEFT_TO_RIGHT_MARK}{body}"
        return f"{stamp} - {body}"

    def media_marker(self, kind: MediaKind, sequence: int, timestamp: datetime) -> str:
        if self.platform is Platform.ANDROID:
            return self.media_omitted
        name = f"{sequence:08d}-{IOS_ATTACHMENT_TOKENS[kind]}-{timestamp:%Y-%m-%d-%H-%M-%S}"
        return LEFT_TO_RIGHT_MARK + self.attachment.format(name=name + IOS_EXTENSIONS[kind])

    def deleted_marker(self) -> str:
        if self.platform is Platform.IOS:
            return LEFT_TO_RIGHT_MARK + self.deleted
        return self.deleted


IOS_ATTACHMENT_TOKENS = {
    MediaKind.IMAGE: "PHOTO",
    MediaKind.VIDEO: "VIDEO",
    MediaKind.AUDIO: "AUDIO",
    MediaKind.STICKER: "STICKER",
    MediaKind.DOCUMENT: "DOCUMENT",
}
IOS_EXTENSIONS = {
    MediaKind.IMAGE: ".jpg",
    MediaKind.VIDEO: ".mp4",
    MediaKind.AUDIO: ".opus",
    MediaKind.STICKER: ".webp",
    MediaKind.DOCUMENT: ".pdf",
}

_ENCRYPTION_NOTICE = {
    "en": (
        "Messages and calls are end-to-end encrypted. No one outside of this chat, not even "
        "WhatsApp, can read or listen to them."
    ),
    "pt-BR": (
        "As mensagens e as chamadas são protegidas com a criptografia de ponta a ponta. Ninguém "
        "fora desta conversa, nem mesmo o WhatsApp, pode ler ou ouvi-las."
    ),
}

EXPORT_FORMATS = {
    fmt.id: fmt
    for fmt in (
        ExportFormat(
            id="android-en",
            platform=Platform.ANDROID,
            locale="en",
            day_first=False,
            four_digit_year=False,
            twelve_hour_clock=True,
            has_seconds=False,
            date_time_separator=", ",
            media_omitted="<Media omitted>",
            attachment="",
            deleted="This message was deleted",
            encryption_notice=_ENCRYPTION_NOTICE["en"],
        ),
        ExportFormat(
            id="android-pt-BR",
            platform=Platform.ANDROID,
            locale="pt-BR",
            day_first=True,
            four_digit_year=True,
            twelve_hour_clock=False,
            has_seconds=False,
            date_time_separator=" ",
            media_omitted="<Mídia oculta>",
            attachment="",
            deleted="Mensagem apagada",
            encryption_notice=_ENCRYPTION_NOTICE["pt-BR"],
        ),
        ExportFormat(
            id="ios-en",
            platform=Platform.IOS,
            locale="en",
            day_first=False,
            four_digit_year=False,
            twelve_hour_clock=True,
            has_seconds=True,
            date_time_separator=", ",
            media_omitted="",
            attachment="<attached: {name}>",
            deleted="This message was deleted.",
            encryption_notice=_ENCRYPTION_NOTICE["en"],
        ),
        ExportFormat(
            id="ios-pt-BR",
            platform=Platform.IOS,
            locale="pt-BR",
            day_first=True,
            four_digit_year=True,
            twelve_hour_clock=False,
            has_seconds=True,
            date_time_separator=", ",
            media_omitted="",
            attachment="<anexado: {name}>",
            deleted="Mensagem apagada",
            encryption_notice=_ENCRYPTION_NOTICE["pt-BR"],
        ),
    )
}


def formats_for_locale(locale: str) -> list[ExportFormat]:
    return [fmt for fmt in EXPORT_FORMATS.values() if fmt.locale == locale]
