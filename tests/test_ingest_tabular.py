"""CSV and XLSX adapters with column mappings, using synthetic data only."""

import csv
import io
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from whatsapp_analyzer.ingest import tabular
from whatsapp_analyzer.ingest.mapping import (
    CANONICAL,
    IMAZING_CSV,
    load_mapping_file,
    parse_overrides,
)
from whatsapp_analyzer.ingest.model import (
    InputError,
    InputErrorCode,
    IssueCode,
    Message,
    RecordIssue,
)
from whatsapp_analyzer.ingest.tabular import TabularSettings
from whatsapp_analyzer.schema import CANONICAL_COLUMNS, Direction, MediaKind, MessageType

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
IMAZING_HEADER = [
    "Chat Session",
    "Message Date",
    "Sent Date",
    "Type",
    "Sender ID",
    "Sender Name",
    "Status",
    "Forwarded",
    "Replying to",
    "Text",
    "Reactions",
    "Attachment",
    "Attachment type",
    "Attachment info",
]


def csv_bytes(rows: list[list[str]], delimiter: str = ",") -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer, delimiter=delimiter, lineterminator="\n").writerows(rows)
    return buffer.getvalue().encode("utf-8")


def parse_csv(path: Path, **settings: object) -> tuple[list[Message], list[RecordIssue]]:
    items = list(tabular.parse_csv(path, TabularSettings(owner_label="me", **settings)))  # type: ignore[arg-type]
    return [i for i in items if isinstance(i, Message)], [
        i for i in items if isinstance(i, RecordIssue)
    ]


def parse_xlsx(path: Path, **settings: object) -> tuple[list[Message], list[RecordIssue]]:
    items = list(tabular.parse_xlsx(path, TabularSettings(owner_label="me", **settings)))  # type: ignore[arg-type]
    return [i for i in items if isinstance(i, Message)], [
        i for i in items if isinstance(i, RecordIssue)
    ]


def canonical_file(tmp_path: Path, *rows: list[str], name: str = "data.csv") -> Path:
    path = tmp_path / name
    path.write_bytes(csv_bytes([list(CANONICAL_COLUMNS), *rows]))
    return path


ROW = ["c1", "2026-01-05T14:07:09", "Alex", "inbound", "hello", "text", ""]


def test_canonical_csv_is_detected_and_parsed(tmp_path: Path) -> None:
    messages, issues = parse_csv(canonical_file(tmp_path, ROW))
    assert issues == []
    assert messages == [
        Message(
            "c1",
            datetime(2026, 1, 5, 14, 7, 9),
            "Alex",
            Direction.INBOUND,
            "hello",
            MessageType.TEXT,
            None,
            2,
        )
    ]


def test_imazing_csv_preset(tmp_path: Path) -> None:
    def row(**values: str) -> list[str]:
        return [values.get(column, "") for column in IMAZING_HEADER]

    path = tmp_path / "export.csv"
    path.write_bytes(
        csv_bytes(
            [
                IMAZING_HEADER,
                row(
                    **{
                        "Chat Session": "Group A",
                        "Message Date": "2024-03-01 10:00:00",
                        "Type": "Incoming",
                        "Sender Name": "Alex",
                        "Text": "hi\nthere",
                    }
                ),
                row(
                    **{
                        "Chat Session": "Group A",
                        "Message Date": "2024-03-01 10:01:00",
                        "Type": "Outgoing",
                        "Text": "ok",
                    }
                ),
                row(
                    **{
                        "Chat Session": "Group A",
                        "Message Date": "2024-03-01 10:02:00",
                        "Type": "Notification",
                        "Text": "Alex joined",
                    }
                ),
                row(
                    **{
                        "Chat Session": "Group A",
                        "Message Date": "2024-03-01 10:03:00",
                        "Type": "Incoming",
                        "Attachment type": "Contact",
                    }
                ),
                row(
                    **{
                        "Chat Session": "Group A",
                        "Message Date": "2024-03-01 10:04:00",
                        "Type": "Incoming",
                        "Attachment type": "Gif",
                    }
                ),
                row(
                    **{
                        "Chat Session": "Group A",
                        "Message Date": "2024-03-01 10:05:00",
                        "Type": "Incoming",
                        "Text": "This message was deleted",
                    }
                ),
            ]
        )
    )
    messages, issues = parse_csv(path)
    assert issues == []
    text, outgoing, notification, contact, gif, deleted = messages
    assert text.text == "hi\nthere"
    assert (outgoing.direction, outgoing.sender) == (Direction.OUTBOUND, "me")
    assert (notification.direction, notification.message_type) == (None, MessageType.SYSTEM)
    assert (contact.message_type, contact.media_kind) == (MessageType.MEDIA, MediaKind.CONTACT)
    assert gif.media_kind is MediaKind.VIDEO
    assert (deleted.message_type, deleted.text) == (MessageType.DELETED, "")


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_delimiter_is_detected(tmp_path: Path, delimiter: str) -> None:
    path = tmp_path / "data.csv"
    path.write_bytes(csv_bytes([list(CANONICAL_COLUMNS), ROW], delimiter=delimiter))
    messages, _ = parse_csv(path)
    assert messages[0].text == "hello"


def test_utf8_bom_and_utf16_are_read(tmp_path: Path) -> None:
    text = csv_bytes(
        [list(CANONICAL_COLUMNS), ["c1", "2026-01-05T14:07:09", "Zoë", "inbound", "olá", "", ""]]
    ).decode()
    bom = tmp_path / "bom.csv"
    bom.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    utf16 = tmp_path / "utf16.csv"
    utf16.write_bytes(text.encode("utf-16"))
    assert parse_csv(bom)[0][0].sender == "Zoë"
    assert parse_csv(utf16)[0][0].text == "olá"


def test_non_utf8_input_needs_an_explicit_encoding(tmp_path: Path) -> None:
    path = tmp_path / "latin.csv"
    path.write_bytes(
        csv_bytes([list(CANONICAL_COLUMNS)])
        + "c1,2026-01-05T14:07:09,Zoë,inbound,olá,,\n".encode("cp1252")
    )
    with pytest.raises(InputError) as error:
        parse_csv(path)
    assert error.value.code is InputErrorCode.UNSUPPORTED_ENCODING
    assert parse_csv(path, encoding="cp1252")[0][0].sender == "Zoë"


def test_unknown_encoding_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InputError) as error:
        parse_csv(canonical_file(tmp_path, ROW), encoding="no-such-codec")
    assert error.value.code is InputErrorCode.UNSUPPORTED_ENCODING


def test_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_bytes(b"")
    with pytest.raises(InputError) as error:
        parse_csv(path)
    assert error.value.code is InputErrorCode.EMPTY_FILE


def test_unrecognized_header_requires_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "other.csv"
    path.write_bytes(csv_bytes([["chat", "when", "who", "body"], ["c1", "2026-01-05", "A", "x"]]))
    with pytest.raises(InputError) as error:
        parse_csv(path)
    assert error.value.code is InputErrorCode.MISSING_REQUIRED_COLUMNS
    assert error.value.details == {"fields": ["conversation_id", "timestamp", "direction"]}


def test_mapping_file_for_the_custom_columns_example() -> None:
    mapping = load_mapping_file(EXAMPLES / "mappings" / "custom-columns.toml")
    messages, issues = parse_csv(
        EXAMPLES / "demo-en" / "conversations_custom_columns.csv", mapping=mapping
    )
    canonical, _ = parse_csv(EXAMPLES / "demo-en" / "conversations.csv")
    assert issues == []
    # That file has no media_kind column, so only that field differs from the canonical file.
    assert [m.key()[:-1] for m in messages] == [m.key()[:-1] for m in canonical]


def test_overrides_complete_a_partial_mapping(tmp_path: Path) -> None:
    path = tmp_path / "other.csv"
    path.write_bytes(
        csv_bytes([["chat", "when", "dir"], ["c1", "2026-01-05T10:00:00", "outbound"]])
    )
    overrides = parse_overrides(["conversation_id=chat", "timestamp=when", "direction=dir"])
    messages, _ = parse_csv(path, overrides=overrides)
    assert (messages[0].conversation_id, messages[0].sender) == ("c1", "me")


@pytest.mark.parametrize("pair", ["timestamp", "nonsense=col", "timestamp="])
def test_invalid_overrides_are_rejected(pair: str) -> None:
    with pytest.raises(InputError) as error:
        parse_overrides([pair])
    assert error.value.code is InputErrorCode.INVALID_COLUMN_MAPPING


def test_mapping_to_a_missing_column_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InputError) as error:
        parse_csv(canonical_file(tmp_path, ROW), overrides={"text": "body"})
    assert error.value.code is InputErrorCode.INVALID_COLUMN_MAPPING
    assert error.value.details == {"columns_not_found": ["body"]}


def test_repeated_mapped_column_is_ambiguous(tmp_path: Path) -> None:
    path = tmp_path / "dup.csv"
    path.write_bytes(csv_bytes([[*CANONICAL_COLUMNS, "text"], [*ROW, "other"]]))
    with pytest.raises(InputError) as error:
        parse_csv(path)
    assert error.value.code is InputErrorCode.INVALID_COLUMN_MAPPING


def test_invalid_mapping_file_lists_the_bad_keys(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(
        '[columns]\nwhen = "x"\n[values.direction]\nyes = "sideways"\n', encoding="utf-8"
    )
    with pytest.raises(InputError) as error:
        load_mapping_file(path)
    assert error.value.details["invalid_keys"] == ["columns.when", "values.direction.yes"]


@pytest.mark.parametrize(
    ("row", "code", "field"),
    [
        (
            ["", "2026-01-05T14:07:09", "A", "inbound", "x", "", ""],
            IssueCode.MISSING_REQUIRED_FIELD,
            "conversation_id",
        ),
        (["c1", "", "A", "inbound", "x", "", ""], IssueCode.MISSING_REQUIRED_FIELD, "timestamp"),
        (
            ["c1", "05/01/2026 14:07", "A", "inbound", "x", "", ""],
            IssueCode.INVALID_TIMESTAMP,
            "timestamp",
        ),
        (
            ["c1", "2026-01-05T14:07:09+02:00", "A", "inbound", "x", "", ""],
            IssueCode.INVALID_TIMESTAMP,
            "timestamp",
        ),
        (
            ["c1", "2026-01-05T14:07:09", "A", "", "x", "text", ""],
            IssueCode.MISSING_REQUIRED_FIELD,
            "direction",
        ),
        (
            ["c1", "2026-01-05T14:07:09", "A", "sideways", "x", "", ""],
            IssueCode.INVALID_DIRECTION,
            "direction",
        ),
        (
            ["c1", "2026-01-05T14:07:09", "A", "inbound", "x", "poll", ""],
            IssueCode.INVALID_VALUE,
            "message_type",
        ),
        (
            ["c1", "2026-01-05T14:07:09", "A", "inbound", "x", "", "hologram"],
            IssueCode.INVALID_VALUE,
            "media_kind",
        ),
    ],
)
def test_invalid_rows_are_reported_and_skipped(
    tmp_path: Path, row: list[str], code: IssueCode, field: str
) -> None:
    messages, issues = parse_csv(canonical_file(tmp_path, row, ROW))
    assert len(messages) == 1
    assert [(i.code, i.field, i.line) for i in issues] == [(code, field, 2)]


def test_system_rows_may_have_no_direction(tmp_path: Path) -> None:
    messages, _ = parse_csv(
        canonical_file(tmp_path, ["c1", "2026-01-05T14:07:09", "", "", "notice", "system", ""])
    )
    assert (messages[0].direction, messages[0].message_type) == (None, MessageType.SYSTEM)


def test_wrong_width_and_empty_rows(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_bytes(
        csv_bytes([list(CANONICAL_COLUMNS), ["c1", "x"], [], ["", "", "", "", "", "", ""], ROW])
    )
    messages, issues = parse_csv(path)
    assert len(messages) == 1
    assert [i.code for i in issues] == [
        IssueCode.MALFORMED_RECORD,
        IssueCode.EMPTY_ROW,
        IssueCode.EMPTY_ROW,
    ]


def test_canonical_import_reverses_formula_escaping(tmp_path: Path) -> None:
    messages, _ = parse_csv(
        canonical_file(tmp_path, ["c1", "2026-01-05T14:07:09", "'=Sam", "inbound", "'=1+1", "", ""])
    )
    assert (messages[0].sender, messages[0].text) == ("=Sam", "=1+1")


def test_other_mappings_keep_leading_apostrophes(tmp_path: Path) -> None:
    path = tmp_path / "other.csv"
    path.write_bytes(
        csv_bytes(
            [["chat", "when", "dir", "body"], ["c1", "2026-01-05T10:00:00", "inbound", "'=1+1"]]
        )
    )
    overrides = parse_overrides(
        ["conversation_id=chat", "timestamp=when", "direction=dir", "text=body"]
    )
    assert parse_csv(path, overrides=overrides)[0][0].text == "'=1+1"


# XLSX ---------------------------------------------------------------------------------------


def workbook_file(tmp_path: Path, sheets: dict[str, list[list[object]]]) -> Path:
    workbook = Workbook()
    assert workbook.active is not None
    workbook.remove(workbook.active)
    for title, rows in sheets.items():
        sheet = workbook.create_sheet(title)
        for row in rows:
            sheet.append(row)
    path = tmp_path / "data.xlsx"
    workbook.save(path)
    return path


XLSX_ROW: list[object] = [
    "c1",
    datetime(2026, 1, 5, 14, 7, 9),
    "Alex",
    "inbound",
    "hello",
    None,
    None,
]


def test_xlsx_header_below_a_title_row_is_found() -> None:
    messages, issues = parse_xlsx(EXAMPLES / "demo-en" / "conversations.xlsx")
    canonical, _ = parse_csv(EXAMPLES / "demo-en" / "conversations.csv")
    assert issues == []
    assert [m.key() for m in messages] == [m.key() for m in canonical]
    assert messages[0].line == 4


def test_xlsx_explicit_header_row(tmp_path: Path) -> None:
    path = workbook_file(tmp_path, {"Data": [["title"], list(CANONICAL_COLUMNS), XLSX_ROW]})
    assert parse_xlsx(path, header_row=2)[0][0].timestamp == datetime(2026, 1, 5, 14, 7, 9)
    with pytest.raises(InputError) as error:
        parse_xlsx(path, header_row=1)
    assert error.value.code is InputErrorCode.MISSING_REQUIRED_COLUMNS


def test_xlsx_sheet_selection(tmp_path: Path) -> None:
    path = workbook_file(
        tmp_path, {"Notes": [["nothing here"]], "Data": [list(CANONICAL_COLUMNS), XLSX_ROW]}
    )
    assert len(parse_xlsx(path)[0]) == 1
    assert len(parse_xlsx(path, sheet="Data")[0]) == 1
    with pytest.raises(InputError) as error:
        parse_xlsx(path, sheet="Missing")
    assert error.value.code is InputErrorCode.SHEET_NOT_FOUND


def test_xlsx_with_two_data_sheets_is_ambiguous(tmp_path: Path) -> None:
    rows: list[list[object]] = [list(CANONICAL_COLUMNS), XLSX_ROW]
    path = workbook_file(tmp_path, {"A": rows, "B": rows})
    with pytest.raises(InputError) as error:
        parse_xlsx(path)
    assert error.value.code is InputErrorCode.AMBIGUOUS_SHEET


def test_xlsx_without_a_recognizable_header(tmp_path: Path) -> None:
    path = workbook_file(tmp_path, {"Data": [["a", "b"], [1, 2]]})
    with pytest.raises(InputError) as error:
        parse_xlsx(path)
    assert error.value.code is InputErrorCode.HEADER_NOT_FOUND


def test_xlsx_formulas_are_not_evaluated(tmp_path: Path) -> None:
    row = list(XLSX_ROW)
    row[4] = "=1+1"
    path = workbook_file(tmp_path, {"Data": [list(CANONICAL_COLUMNS), row]})
    # A formula cell without a cached value reads as empty; it is never computed.
    assert parse_xlsx(path)[0][0].text == ""


def test_corrupted_spreadsheet(tmp_path: Path) -> None:
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"PK\x03\x04 definitely not a workbook")
    with pytest.raises(InputError) as error:
        parse_xlsx(path)
    assert error.value.code is InputErrorCode.SPREADSHEET_CORRUPTED


def test_presets_have_the_documented_columns() -> None:
    assert set(CANONICAL.columns) == set(CANONICAL_COLUMNS)
    assert set(IMAZING_CSV.columns.values()) <= set(IMAZING_HEADER)


def test_xlsx_boolean_cells_match_true_false_value_maps(tmp_path: Path) -> None:
    path = workbook_file(
        tmp_path,
        {
            "Data": [
                ["chat", "when", "mine"],
                ["c1", datetime(2026, 1, 5, 10, 0), True],
                ["c1", datetime(2026, 1, 5, 10, 1), False],
            ]
        },
    )
    mapping = tmp_path / "map.toml"
    mapping.write_text(
        '[columns]\nconversation_id = "chat"\ntimestamp = "when"\ndirection = "mine"\n'
        '[values.direction]\n"true" = "outbound"\n"false" = "inbound"\n',
        encoding="utf-8",
    )
    messages, issues = parse_xlsx(path, mapping=load_mapping_file(mapping))
    assert issues == []
    assert [m.direction for m in messages] == [Direction.OUTBOUND, Direction.INBOUND]


def test_mapping_file_with_a_non_table_timestamp_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text('timestamp = "x"\n[columns]\nconversation_id = "c"\n', encoding="utf-8")
    with pytest.raises(InputError) as error:
        load_mapping_file(path)
    assert error.value.details["invalid_keys"] == ["timestamp"]
