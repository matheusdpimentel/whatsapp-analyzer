"""Native WhatsApp TXT adapter: hand-written fixtures plus a cross-check with the generator."""

from datetime import datetime
from pathlib import Path

import pytest

from whatsapp_analyzer.export_formats import EXPORT_FORMATS
from whatsapp_analyzer.ingest import whatsapp_txt
from whatsapp_analyzer.ingest.model import (
    InputError,
    InputErrorCode,
    IssueCode,
    Message,
    RecordIssue,
)
from whatsapp_analyzer.ingest.whatsapp_txt import DateOrder
from whatsapp_analyzer.schema import Direction, MediaKind, MessageType
from whatsapp_analyzer.synthetic.generator import GeneratorSettings, generate
from whatsapp_analyzer.synthetic.writers import expected_txt_records, render_txt

LRM = "\u200e"
NNBSP = "\u202f"
OWNER = "Northwind Support"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "chat.txt"
    path.write_text(text, encoding="utf-8")
    return path


def parse(
    path: Path, owner: str | None = OWNER, order: DateOrder | None = None
) -> tuple[list[Message], list[RecordIssue]]:
    items = list(whatsapp_txt.parse(path, "utf-8-sig", owner, order))
    return (
        [i for i in items if isinstance(i, Message)],
        [i for i in items if isinstance(i, RecordIssue)],
    )


ANDROID_EN = (
    f"1/28/26, 6:00{NNBSP}PM - Messages and calls are end-to-end encrypted.\n"
    f"1/28/26, 6:00{NNBSP}PM - Alex Rivera: Hello, my order is late.\n"
    f"1/28/26, 6:03{NNBSP}PM - Northwind Support: Could you send the details?\n"
    f"1/28/26, 6:05{NNBSP}PM - Alex Rivera: Here are the details:\n"
    "- order 48213\n"
    "12/03 works for the delivery\n"
    f"1/28/26, 6:14{NNBSP}PM - Alex Rivera: <Media omitted>\n"
    f"1/28/26, 6:15{NNBSP}PM - Alex Rivera: This message was deleted\n"
    f"1/29/26, 12:05{NNBSP}AM - Northwind Support: ok\n"
)

IOS_PT = (
    f"[27/01/2025, 12:28:17] Ana Ribeiro: {LRM}As mensagens são protegidas.\n"
    "[27/01/2025, 12:28:17] Ana Ribeiro: Oi! Consigo mudar o endereço?\n"
    "[27/01/2025, 12:29:58] Suporte Exemplo: certo\n"
    f"[27/01/2025, 12:30:00] Ana Ribeiro: {LRM}<anexado: 00000001-PHOTO-2025-01-27-12-30-00.jpg>\n"
    "Segue a foto da etiqueta\n"
    f"[27/01/2025, 12:31:00] Ana Ribeiro: {LRM}<anexado: 00000002-Contrato final.pdf>\n"
    f"[27/01/2025, 12:32:00] Ana Ribeiro: {LRM}Mensagem apagada\n"
)


def test_android_english_export(tmp_path: Path) -> None:
    messages, issues = parse(write(tmp_path, ANDROID_EN))
    assert issues == []
    system, first, reply, multiline, media, deleted, late = messages
    assert (system.message_type, system.direction, system.sender) == (MessageType.SYSTEM, None, "")
    assert first.timestamp == datetime(2026, 1, 28, 18, 0)
    assert (first.sender, first.direction) == ("Alex Rivera", Direction.INBOUND)
    assert reply.direction is Direction.OUTBOUND
    assert multiline.text == "Here are the details:\n- order 48213\n12/03 works for the delivery"
    assert (media.message_type, media.media_kind, media.text) == (MessageType.MEDIA, None, "")
    assert (deleted.message_type, deleted.text) == (MessageType.DELETED, "")
    assert late.timestamp == datetime(2026, 1, 29, 0, 5)


def test_ios_portuguese_export(tmp_path: Path) -> None:
    messages, _ = parse(write(tmp_path, IOS_PT), owner="Suporte Exemplo")
    system, text, reply, photo, document, deleted = messages
    assert system.message_type is MessageType.SYSTEM
    assert text.timestamp == datetime(2025, 1, 27, 12, 28, 17)
    assert reply.direction is Direction.OUTBOUND
    assert (photo.media_kind, photo.text) == (MediaKind.IMAGE, "Segue a foto da etiqueta")
    assert document.media_kind is MediaKind.DOCUMENT
    assert deleted.message_type is MessageType.DELETED


def test_android_caption_after_media_marker_is_kept(tmp_path: Path) -> None:
    text = "05/01/2026 14:07 - Ana: <Mídia oculta>\nlegenda da foto\n05/01/2026 14:08 - Eu: ok\n"
    messages, _ = parse(write(tmp_path, text), owner="Eu", order=DateOrder.DAY_FIRST)
    assert (messages[0].message_type, messages[0].text) == (MessageType.MEDIA, "legenda da foto")


def test_ambiguous_dates_are_rejected_without_date_order(tmp_path: Path) -> None:
    path = write(tmp_path, "05/01/2026 14:07 - Ana: oi\n06/01/2026 09:00 - Eu: ok\n")
    with pytest.raises(InputError) as error:
        parse(path, owner="Eu")
    assert error.value.code is InputErrorCode.AMBIGUOUS_DATE_ORDER


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        (DateOrder.DAY_FIRST, datetime(2026, 1, 5, 14, 7)),
        (DateOrder.MONTH_FIRST, datetime(2026, 5, 1, 14, 7)),
    ],
)
def test_explicit_date_order_resolves_ambiguity(
    tmp_path: Path, order: DateOrder, expected: datetime
) -> None:
    path = write(tmp_path, "05/01/2026 14:07 - Ana: oi\n06/01/2026 09:00 - Eu: ok\n")
    messages, _ = parse(path, owner="Eu", order=order)
    assert messages[0].timestamp == expected


def test_one_unambiguous_date_settles_the_whole_file(tmp_path: Path) -> None:
    path = write(tmp_path, "05/01/2026 14:07 - Ana: oi\n25/01/2026 09:00 - Eu: ok\n")
    messages, _ = parse(path, owner="Eu")
    assert messages[0].timestamp == datetime(2026, 1, 5, 14, 7)


def test_mixed_date_orders_are_rejected(tmp_path: Path) -> None:
    path = write(tmp_path, "25/01/2026 14:07 - Ana: oi\n01/25/2026 09:00 - Eu: ok\n")
    with pytest.raises(InputError) as error:
        parse(path, owner="Eu")
    assert error.value.code is InputErrorCode.MIXED_DATE_FORMATS


def test_year_first_and_dotted_dates(tmp_path: Path) -> None:
    messages, _ = parse(
        write(tmp_path, "2026.01.05 14:07 - Ana: oi\n2026.01.05 14:08 - Eu: ok\n"), owner="Eu"
    )
    assert messages[0].timestamp == datetime(2026, 1, 5, 14, 7)


def test_owner_is_required(tmp_path: Path) -> None:
    with pytest.raises(InputError) as error:
        parse(write(tmp_path, ANDROID_EN), owner=None)
    assert error.value.code is InputErrorCode.OWNER_REQUIRED


def test_owner_absent_from_the_file_is_a_warning(tmp_path: Path) -> None:
    messages, issues = parse(write(tmp_path, ANDROID_EN), owner="Someone Else")
    assert [(i.code, i.line) for i in issues] == [(IssueCode.OWNER_NOT_IN_FILE, 0)]
    assert {m.direction for m in messages if m.direction} == {Direction.INBOUND}


def test_invalid_twelve_hour_time_is_reported(tmp_path: Path) -> None:
    text = f"1/28/26, 13:00{NNBSP}PM - Alex: bad\n1/28/26, 1:00{NNBSP}PM - Northwind Support: ok\n"
    messages, issues = parse(write(tmp_path, text))
    assert len(messages) == 1
    assert [i.code for i in issues] == [IssueCode.INVALID_TIMESTAMP]


def test_text_before_the_first_header_is_reported(tmp_path: Path) -> None:
    messages, issues = parse(write(tmp_path, "garbage\n" + ANDROID_EN))
    assert len(messages) == 7
    assert [(i.code, i.line) for i in issues] == [(IssueCode.MALFORMED_RECORD, 1)]


def test_looks_like() -> None:
    assert whatsapp_txt.looks_like(["", f"1/28/26, 6:00{NNBSP}PM - Alex: hi"])
    assert whatsapp_txt.looks_like(["[27/01/2025, 12:28:17] Ana: oi"])
    assert not whatsapp_txt.looks_like(["hello", "world"])


@pytest.mark.parametrize("format_id", sorted(EXPORT_FORMATS))
def test_parsing_generated_exports_recovers_the_expected_records(
    tmp_path: Path, format_id: str
) -> None:
    fmt = EXPORT_FORMATS[format_id]
    for conversation in generate(GeneratorSettings(seed=3, conversations=7, locale=fmt.locale)):
        path = tmp_path / f"{conversation.conversation_id}.txt"
        path.write_text(render_txt(conversation, fmt), encoding="utf-8")
        # Short conversations can be ambiguous on their own; pass the order as a user would.
        order = DateOrder.DAY_FIRST if fmt.day_first else DateOrder.MONTH_FIRST
        messages, issues = parse(path, owner=conversation.owner, order=order)
        assert [i for i in issues if i.code is not IssueCode.MALFORMED_RECORD] == []
        parsed = [
            {
                "timestamp": m.timestamp.isoformat(),
                "sender": m.sender,
                "direction": m.direction.value if m.direction else "",
                "text": m.text,
                "message_type": m.message_type.value,
                "media_kind": m.media_kind.value if m.media_kind else "",
            }
            for m in messages
        ]
        expected = [
            {key: value for key, value in row.items() if key != "conversation_id"}
            for row in expected_txt_records(conversation, fmt)
        ]
        assert parsed == expected, f"{format_id} {conversation.conversation_id}"
