import csv
import io
import os
import sys
import tracemalloc
from pathlib import Path

import pytest

from whatsapp_analyzer.ingest.detect import collect_inputs, detect_format
from whatsapp_analyzer.ingest.model import (
    InputError,
    InputErrorCode,
    InputFormat,
    IssueCode,
    Message,
)
from whatsapp_analyzer.ingest.pipeline import ImportOptions, run_import
from whatsapp_analyzer.ingest.report import ImportReport
from whatsapp_analyzer.schema import CANONICAL_COLUMNS

SEP = "-" * 52
ROW = ["c1", "2026-01-05T14:07:09", "Alex", "inbound", "ok", "text", ""]


def write_csv(path: Path, *rows: list[str]) -> Path:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows([list(CANONICAL_COLUMNS), *rows])
    path.write_text(buffer.getvalue(), encoding="utf-8")
    return path


def collect(paths: list[Path], **options: object) -> tuple[list[Message], ImportReport]:
    messages: list[Message] = []
    report = run_import(paths, ImportOptions(**options), sink=messages.append)  # type: ignore[arg-type]
    return messages, report


def test_duplicates_are_kept_and_reported_by_default(tmp_path: Path) -> None:
    messages, report = collect([write_csv(tmp_path / "a.csv", ROW, ROW)])
    assert len(messages) == 2
    assert report.totals()["duplicates"] == 1
    assert report.issue_counts()[IssueCode.DUPLICATE_RECORD] == 1


def test_duplicates_can_be_dropped(tmp_path: Path) -> None:
    messages, report = collect([write_csv(tmp_path / "a.csv", ROW, ROW)], drop_duplicates=True)
    assert len(messages) == 1
    assert report.totals()["records_written"] == 1


def test_duplicates_are_detected_across_files(tmp_path: Path) -> None:
    _, report = collect([write_csv(tmp_path / "a.csv", ROW), write_csv(tmp_path / "b.csv", ROW)])
    assert report.totals()["duplicates"] == 1


def test_out_of_order_timestamps_keep_source_order(tmp_path: Path) -> None:
    later = list(ROW)
    later[1] = "2026-01-06T00:00:00"
    messages, report = collect([write_csv(tmp_path / "a.csv", later, ROW)])
    assert [m.timestamp.day for m in messages] == [6, 5]
    assert report.issue_counts()[IssueCode.TIMESTAMP_OUT_OF_ORDER] == 1


def test_control_characters_are_removed_and_reported(tmp_path: Path) -> None:
    row = list(ROW)
    row[4] = "a\x02b\x00c 👍🏽 ‎ok"
    messages, report = collect([write_csv(tmp_path / "a.csv", row)])
    assert messages[0].text == "abc 👍🏽 ‎ok"
    assert report.issue_counts()[IssueCode.CONTROL_CHARACTERS_REMOVED] == 1


def test_file_without_valid_records_is_rejected(tmp_path: Path) -> None:
    bad = list(ROW)
    bad[3] = "sideways"
    _, report = collect([write_csv(tmp_path / "a.csv", bad)])
    assert [f.error for f in report.files] == [InputErrorCode.NO_VALID_RECORDS]


def test_missing_file_is_reported(tmp_path: Path) -> None:
    _, report = collect([tmp_path / "missing.csv"])
    assert report.files[0].error is InputErrorCode.FILE_NOT_FOUND


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permissions as non-root"
)
def test_unreadable_file_is_reported(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "a.csv", ROW)
    path.chmod(0)
    try:
        _, report = collect([path])
    finally:
        path.chmod(0o600)
    assert report.files[0].error is InputErrorCode.PERMISSION_DENIED


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("a.txt", "", InputErrorCode.EMPTY_FILE),
        ("a.txt", "just some notes\n", InputErrorCode.UNSUPPORTED_FORMAT),
        ("a.json", "{}", InputErrorCode.UNSUPPORTED_FORMAT),
    ],
)
def test_detection_errors(
    tmp_path: Path, name: str, content: str, expected: InputErrorCode
) -> None:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    with pytest.raises(InputError) as error:
        detect_format(path, None)
    assert error.value.code is expected


def test_detection_of_txt_formats(tmp_path: Path) -> None:
    imazing = tmp_path / "a.txt"
    imazing.write_text(f"{SEP}\nAlex\n2024-03-01 10:00:00 to Alex\n\nhi\n", encoding="utf-8")
    native = tmp_path / "b.txt"
    native.write_text("[27/01/2025, 12:28:17] Ana: oi\n", encoding="utf-8")
    assert detect_format(imazing, None) is InputFormat.IMAZING_TXT
    assert detect_format(native, None) is InputFormat.WHATSAPP_TXT


def test_directories_expand_to_supported_files_in_name_order(tmp_path: Path) -> None:
    for name in ("b.csv", "a.txt", "c.xlsx", "notes.md", ".hidden.csv"):
        (tmp_path / name).write_text("", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    assert [p.name for p in collect_inputs([tmp_path])] == ["a.txt", "b.csv", "c.xlsx"]


def test_parsing_is_deterministic() -> None:
    examples = Path(__file__).resolve().parent.parent / "examples" / "demo-pt-BR"
    first, _ = collect([examples / "conversations.csv"])
    second, _ = collect([examples / "conversations.csv"])
    assert first == second


def test_large_csv_is_streamed(tmp_path: Path) -> None:
    path = tmp_path / "large.csv"
    rows = 100_000
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(CANONICAL_COLUMNS)
        for index in range(rows):
            writer.writerow(
                [
                    f"c{index % 50}",
                    f"2026-01-05T{index % 24:02d}:00:00",
                    "Alex",
                    "inbound",
                    f"message {index} " * 8,
                    "text",
                    "",
                ]
            )
    count = 0

    def sink(_: Message) -> None:
        nonlocal count
        count += 1

    tracemalloc.start()
    report = run_import([path], ImportOptions(), sink=sink)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert count == rows
    assert report.totals()["records_valid"] == rows
    # Holding every message would need well over 50 MB here; streaming keeps only digests.
    assert peak < 30 * 1024 * 1024, f"peak traced memory {peak / 2**20:.1f} MiB"


def test_txt_files_with_the_same_name_are_not_merged(tmp_path: Path) -> None:
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "_chat.txt").write_text(
            f"{SEP}\nAlex\n2024-03-01 10:00:00 to Alex\n\nhi\n", encoding="utf-8"
        )
    _, report = collect([tmp_path / "a" / "_chat.txt", tmp_path / "b" / "_chat.txt"])
    assert [f.error for f in report.files] == [None, InputErrorCode.CONVERSATION_ID_CONFLICT]
    assert report.files[1].error_details == {"same_name_as_file": 1}


def test_forced_format_only_reads_matching_files_from_directories(tmp_path: Path) -> None:
    write_csv(tmp_path / "a.csv", ROW)
    (tmp_path / "b.txt").write_text("notes", encoding="utf-8")
    assert [p.name for p in collect_inputs([tmp_path], InputFormat.CSV)] == ["a.csv"]


def test_a_file_reached_twice_is_imported_once(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "a.csv", ROW)
    assert collect_inputs([path, tmp_path]) == [path]


def test_rejected_files_still_report_their_findings(tmp_path: Path) -> None:
    bad = list(ROW)
    bad[3] = "sideways"
    _, report = collect([write_csv(tmp_path / "a.csv", bad)])
    data = report.to_dict(include_file_names=False)["files"][0]  # type: ignore[index]
    assert data["error"] == {"code": "NO_VALID_RECORDS"}
    assert data["issues"] == {"INVALID_DIRECTION": 1}
    assert data["issue_lines"] == {"INVALID_DIRECTION": [2]}
