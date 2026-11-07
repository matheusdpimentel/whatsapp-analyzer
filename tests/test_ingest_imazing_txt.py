"""iMazing TXT adapter, tested against hand-written synthetic files (no generator involved)."""

from datetime import datetime
from pathlib import Path

import pytest

from whatsapp_analyzer.ingest import imazing_txt
from whatsapp_analyzer.ingest.model import IssueCode, Message, RecordIssue
from whatsapp_analyzer.schema import Direction, MediaKind, MessageType

SEP = "-" * 52
TITLE = "Alex Rivera"


def record(header: str, *body: str, title: str = TITLE) -> str:
    return "\n".join([SEP, title, header, "", *body, "", ""])


def write(tmp_path: Path, *records: str, newline: str = "\n", bom: bool = True) -> Path:
    text = "\n".join(records).replace("\n", newline)
    path = tmp_path / "chat.txt"
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return path


def parse(path: Path) -> tuple[list[Message], list[RecordIssue]]:
    items = list(imazing_txt.parse(path, "utf-8-sig", owner_label="me"))
    return (
        [i for i in items if isinstance(i, Message)],
        [i for i in items if isinstance(i, RecordIssue)],
    )


def only(
    tmp_path: Path, *body: str, header: str = "2024-03-01 10:00:00 from Alex Rivera (+1 555 0100)"
) -> Message:
    messages, _ = parse(write(tmp_path, record(header, *body)))
    assert len(messages) == 1
    return messages[0]


def test_inbound_message_strips_the_phone_from_the_sender(tmp_path: Path) -> None:
    message = only(tmp_path, "Hello there")
    assert message.sender == "Alex Rivera"
    assert message.direction is Direction.INBOUND
    assert message.timestamp == datetime(2024, 3, 1, 10, 0, 0)
    assert message.text == "Hello there"
    assert message.message_type is MessageType.TEXT
    assert message.conversation_id == "chat"


def test_outbound_message_uses_the_owner_label(tmp_path: Path) -> None:
    message = only(tmp_path, "Hi", header=f"2024-03-01 10:00:00 to {TITLE}")
    assert (message.direction, message.sender) == (Direction.OUTBOUND, "me")


def test_sender_without_phone_is_kept_whole(tmp_path: Path) -> None:
    assert (
        only(tmp_path, "x", header="2024-03-01 10:00:00 from +1 555 0100").sender == "+1 555 0100"
    )
    assert only(tmp_path, "x", header="2024-03-01 10:00:00 from Sam (work)").sender == "Sam (work)"


def test_multiline_text_and_inner_blank_lines_are_preserved(tmp_path: Path) -> None:
    assert only(tmp_path, "line one", "", "line three").text == "line one\n\nline three"


def test_dash_lines_inside_a_message_do_not_split_it(tmp_path: Path) -> None:
    message = only(tmp_path, "before", SEP, "not a header", "-----", "after")
    assert message.text == f"before\n{SEP}\nnot a header\n-----\nafter"


def test_reply_quote_is_removed_and_reported(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        record(
            "2024-03-01 10:00:00 to Alex Rivera",
            "↩ Reply to Alex Rivera, 2024-02-28 09:00:00: first quoted line",
            "second quoted line",
            "",
            "the actual answer",
        ),
    )
    messages, issues = parse(path)
    assert messages[0].text == "the actual answer"
    assert [i.code for i in issues] == [IssueCode.REPLY_QUOTE_REMOVED]


def test_reactions_are_removed_and_reported(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        record(
            "2024-03-01 10:00:00 from Alex Rivera (+1 555 0100)",
            "nice",
            "",
            "Jamie Chen 👍 on 01/03/24, 10:05",
            "Morgan Patel ❤️ on 01/03/24, 10:06",
        ),
    )
    messages, issues = parse(path)
    assert messages[0].text == "nice"
    assert [i.code for i in issues] == [IssueCode.REACTIONS_REMOVED]


def test_text_that_merely_ends_like_a_reaction_is_kept(tmp_path: Path) -> None:
    message = only(tmp_path, "meeting moved to 01/03/24, 10:05")
    assert message.text == "meeting moved to 01/03/24, 10:05"


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (["PTT-20240301-WA0001.opus (Audio)", "00:12"], MediaKind.AUDIO),
        (["IMG-20240301-WA0002.jpg (Image)", "245 KB"], MediaKind.IMAGE),
        (["STK-20240301-WA0003.webp (Sticker)"], MediaKind.STICKER),
        (["VID-20240301-WA0004.mp4 (Video)", "00:31", "2.4 MB"], MediaKind.VIDEO),
        (["GIF-20240301-WA0005.mp4 (Gif)"], MediaKind.VIDEO),
        (["Quarterly report.pdf", "3 pages · 120 KB"], MediaKind.DOCUMENT),
    ],
)
def test_attachment_lines_become_media_without_text(
    tmp_path: Path, body: list[str], kind: MediaKind
) -> None:
    message = only(tmp_path, *body)
    assert (message.message_type, message.media_kind, message.text) == (MessageType.MEDIA, kind, "")


def test_caption_before_the_attachment_is_kept_as_text(tmp_path: Path) -> None:
    message = only(tmp_path, "Here is the label", "IMG-20240301-WA0002.jpg (Image)", "245 KB")
    assert (message.media_kind, message.text) == (MediaKind.IMAGE, "Here is the label")


def test_duration_like_text_without_attachment_stays_text(tmp_path: Path) -> None:
    assert only(tmp_path, "12:30").text == "12:30"
    assert only(tmp_path, "costs 12 KB").message_type is MessageType.TEXT


@pytest.mark.parametrize("marker", ["This message was deleted", "You deleted this message"])
def test_deleted_markers(tmp_path: Path, marker: str) -> None:
    message = only(tmp_path, marker)
    assert (message.message_type, message.text) == (MessageType.DELETED, "")


def test_waiting_placeholder_is_kept_as_text(tmp_path: Path) -> None:
    assert only(tmp_path, "(Waiting for this message)").message_type is MessageType.TEXT


def test_header_without_body_is_an_empty_message(tmp_path: Path) -> None:
    messages, issues = parse(write(tmp_path, record("2024-03-01 10:00:00 to Alex Rivera")))
    assert messages[0].text == ""
    assert [i.code for i in issues] == [IssueCode.EMPTY_MESSAGE]


def test_impossible_date_is_an_invalid_timestamp(tmp_path: Path) -> None:
    messages, issues = parse(write(tmp_path, record("2024-02-30 10:00:00 to Alex Rivera", "x")))
    assert messages == []
    assert [(i.code, i.field) for i in issues] == [(IssueCode.INVALID_TIMESTAMP, "timestamp")]


def test_text_before_the_first_record_is_reported_once(tmp_path: Path) -> None:
    path = write(tmp_path, "stray line\nanother", record("2024-03-01 10:00:00 to Alex Rivera", "x"))
    messages, issues = parse(path)
    assert len(messages) == 1
    assert [(i.code, i.line) for i in issues] == [(IssueCode.MALFORMED_RECORD, 1)]


def test_title_change_is_reported(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        record("2024-03-01 10:00:00 to Alex Rivera", "x"),
        record("2024-03-02 10:00:00 to Alex R.", "y", title="Alex R."),
    )
    messages, issues = parse(path)
    assert len(messages) == 2
    assert [i.code for i in issues] == [IssueCode.TITLE_CHANGED]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("bom", [True, False])
def test_line_endings_and_bom_do_not_change_the_result(
    tmp_path: Path, newline: str, bom: bool
) -> None:
    path = write(
        tmp_path,
        record("2024-03-01 10:00:00 from Alex Rivera (+1 555 0100)", "a", "b"),
        newline=newline,
        bom=bom,
    )
    messages, _ = parse(path)
    assert messages[0].text == "a\nb"


def test_records_keep_their_source_line(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        record("2024-03-01 10:00:00 to Alex Rivera", "x"),
        record("2024-03-01 10:01:00 to Alex Rivera", "y"),
    )
    messages, _ = parse(path)
    assert [m.line for m in messages] == [3, 10]


def test_looks_like() -> None:
    assert imazing_txt.looks_like([SEP, TITLE, "2024-03-01 10:00:00 to Alex Rivera"])
    assert not imazing_txt.looks_like(["1/5/26, 2:07 PM - Alex: hi"])
    assert not imazing_txt.looks_like([SEP, TITLE, "not a header"])


@pytest.mark.parametrize(
    "line",
    ["Fica para 10/03/24, 14:00", "Reunião - amanhã 10/03/24, 14:00", "Ok! Ate 10/03/24, 14:00"],
)
def test_sentence_after_a_blank_line_is_not_taken_for_a_reaction(tmp_path: Path, line: str) -> None:
    message = only(tmp_path, "Pauta", "", line)
    assert message.text == f"Pauta\n\n{line}"


@pytest.mark.parametrize("emoji", ["👍", "👍🏼", "❤️", "🙏", "👨\u200d👩\u200d👧"])
def test_reaction_emoji_variants(tmp_path: Path, emoji: str) -> None:
    message = only(tmp_path, "nice", "", f"Jamie Chen {emoji} on 01/03/24, 10:05")
    assert message.text == "nice"


def test_quote_without_a_blank_line_keeps_all_text(tmp_path: Path) -> None:
    message = only(tmp_path, "↩ Reply to Alex, 2024-03-01 09:00:00: question", "Yes, agreed")
    assert message.text == "↩ Reply to Alex, 2024-03-01 09:00:00: question\nYes, agreed"


@pytest.mark.parametrize(
    "body",
    [["Segue o relatorio.pdf", "2 MB"], ["notes.docx", "18 KB"]],
)
def test_file_name_and_size_alone_stay_text(tmp_path: Path, body: list[str]) -> None:
    message = only(tmp_path, *body)
    assert (message.message_type, message.text) == (MessageType.TEXT, "\n".join(body))


def test_typed_file_name_and_size_with_other_text_stay_text(tmp_path: Path) -> None:
    message = only(tmp_path, "Here it is:", "Segue o relatorio.pdf", "2 MB")
    assert (message.message_type, message.text) == (
        MessageType.TEXT,
        "Here it is:\nSegue o relatorio.pdf\n2 MB",
    )


def test_separator_at_the_end_of_the_file_is_ignored(tmp_path: Path) -> None:
    path = write(tmp_path, record("2024-03-01 10:00:00 to Alex Rivera", "Oi"), SEP)
    messages, _ = parse(path)
    assert messages[0].text == "Oi"
