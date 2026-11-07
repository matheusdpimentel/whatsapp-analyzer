from datetime import datetime

import pytest

from whatsapp_analyzer.export_formats import EXPORT_FORMATS, formats_for_locale
from whatsapp_analyzer.schema import MediaKind

AFTERNOON = datetime(2026, 1, 5, 14, 7, 9)
NNBSP = "\u202f"
LRM = "\u200e"


@pytest.mark.parametrize(
    ("format_id", "expected"),
    [
        ("android-en", f"1/5/26, 2:07{NNBSP}PM"),
        ("android-pt-BR", "05/01/2026 14:07"),
        ("ios-en", f"1/5/26, 2:07:09{NNBSP}PM"),
        ("ios-pt-BR", "05/01/2026, 14:07:09"),
    ],
)
def test_format_timestamp(format_id: str, expected: str) -> None:
    assert EXPORT_FORMATS[format_id].format_timestamp(AFTERNOON) == expected


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (0, f"12:30{NNBSP}AM"),
        (11, f"11:30{NNBSP}AM"),
        (12, f"12:30{NNBSP}PM"),
        (23, f"11:30{NNBSP}PM"),
    ],
)
def test_twelve_hour_clock_boundaries(hour: int, expected: str) -> None:
    stamp = EXPORT_FORMATS["android-en"].format_timestamp(datetime(2026, 1, 5, hour, 30))
    assert stamp.endswith(expected)


def test_android_lines() -> None:
    fmt = EXPORT_FORMATS["android-pt-BR"]
    assert fmt.message_line(AFTERNOON, "Ana", "oi") == "05/01/2026 14:07 - Ana: oi"
    assert fmt.system_line(AFTERNOON, "Ana", "aviso") == "05/01/2026 14:07 - aviso"


def test_ios_lines_attribute_system_notices_to_the_chat() -> None:
    fmt = EXPORT_FORMATS["ios-en"]
    assert fmt.message_line(AFTERNOON, "Alex", "hi").startswith("[1/5/26, 2:07:09")
    assert fmt.system_line(AFTERNOON, "Alex", "notice").endswith(f"] Alex: {LRM}notice")


def test_android_media_marker_does_not_reveal_the_kind() -> None:
    fmt = EXPORT_FORMATS["android-en"]
    assert fmt.media_marker(MediaKind.AUDIO, 1, AFTERNOON) == "<Media omitted>"
    assert fmt.media_marker(MediaKind.IMAGE, 2, AFTERNOON) == "<Media omitted>"


@pytest.mark.parametrize(
    ("kind", "file_name"),
    [
        (MediaKind.IMAGE, "00000003-PHOTO-2026-01-05-14-07-09.jpg"),
        (MediaKind.AUDIO, "00000003-AUDIO-2026-01-05-14-07-09.opus"),
        (MediaKind.DOCUMENT, "00000003-DOCUMENT-2026-01-05-14-07-09.pdf"),
    ],
)
def test_ios_media_marker_names_the_attachment(kind: MediaKind, file_name: str) -> None:
    marker = EXPORT_FORMATS["ios-pt-BR"].media_marker(kind, 3, AFTERNOON)
    assert marker == f"{LRM}<anexado: {file_name}>"


def test_deleted_markers() -> None:
    assert EXPORT_FORMATS["android-en"].deleted_marker() == "This message was deleted"
    assert EXPORT_FORMATS["ios-en"].deleted_marker() == f"{LRM}This message was deleted."


def test_every_locale_has_one_format_per_platform() -> None:
    for locale in ("en", "pt-BR"):
        assert sorted(fmt.platform.value for fmt in formats_for_locale(locale)) == [
            "android",
            "ios",
        ]
