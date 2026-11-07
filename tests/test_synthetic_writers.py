import csv
import io
import json
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook

from whatsapp_analyzer.export_formats import EXPORT_FORMATS, Platform
from whatsapp_analyzer.schema import CANONICAL_COLUMNS
from whatsapp_analyzer.synthetic.generator import GeneratorSettings, generate
from whatsapp_analyzer.synthetic.writers import (
    CUSTOM_COLUMNS,
    XLSX_HEADER_ROW,
    XLSX_SHEET,
    XLSX_TITLE,
    canonical_rows,
    expected_txt_records,
    render_csv,
    render_custom_csv,
    render_txt,
    write_dataset,
    write_xlsx,
)

SETTINGS = GeneratorSettings(seed=5, conversations=8)
CONVERSATIONS = generate(SETTINGS)
EDGE = next(c for c in CONVERSATIONS if c.profile == "edge_cases")


def test_txt_starts_with_the_encryption_notice() -> None:
    fmt = EXPORT_FORMATS["android-en"]
    first_line = render_txt(EDGE, fmt).splitlines()[0]
    assert first_line.endswith(fmt.encryption_notice)


def test_txt_keeps_multiline_messages_as_continuation_lines() -> None:
    lines = render_txt(EDGE, EXPORT_FORMATS["ios-en"]).splitlines()
    assert "- order 48213" in lines


def test_txt_ends_with_a_newline() -> None:
    assert render_txt(EDGE, EXPORT_FORMATS["android-pt-BR"]).endswith("\n")


def test_expected_records_for_android_drop_seconds_and_media_kind() -> None:
    rows = expected_txt_records(EDGE, EXPORT_FORMATS["android-en"])
    assert rows[0]["message_type"] == "system"
    assert rows[0]["direction"] == ""
    assert len(rows) == len(EDGE.messages) + 1
    assert all(row["timestamp"].endswith(":00") for row in rows)
    assert all(row["media_kind"] == "" for row in rows)


def test_expected_records_for_ios_keep_media_kind() -> None:
    rows = expected_txt_records(EDGE, EXPORT_FORMATS["ios-en"])
    kinds = {row["media_kind"] for row in rows if row["message_type"] == "media"}
    assert kinds == {"image", "audio", "video", "sticker", "document"}


def test_canonical_csv_round_trip() -> None:
    reader = csv.DictReader(io.StringIO(render_csv(CONVERSATIONS)))
    assert tuple(reader.fieldnames or ()) == CANONICAL_COLUMNS
    assert list(reader) == canonical_rows(CONVERSATIONS)


def test_custom_csv_uses_other_column_names_and_boolean_direction() -> None:
    reader = csv.DictReader(io.StringIO(render_custom_csv(CONVERSATIONS)))
    assert tuple(reader.fieldnames or ()) == CUSTOM_COLUMNS
    rows = list(reader)
    assert {row["from_me"] for row in rows} == {"true", "false"}
    datetime.strptime(rows[0]["sent_at"], "%d/%m/%Y %H:%M:%S")


def test_xlsx_has_a_title_row_before_the_header(tmp_path: Path) -> None:
    path = tmp_path / "data.xlsx"
    write_xlsx(CONVERSATIONS, path, created=SETTINGS.end)
    sheet = load_workbook(path, read_only=True)[XLSX_SHEET]
    rows = list(sheet.iter_rows(values_only=True))
    assert rows[0][0] == XLSX_TITLE
    assert rows[XLSX_HEADER_ROW - 1] == CANONICAL_COLUMNS
    data = rows[XLSX_HEADER_ROW:]
    assert len(data) == sum(len(c.messages) for c in CONVERSATIONS)
    assert isinstance(data[0][1], datetime)


def test_write_dataset_lists_every_file_it_writes(tmp_path: Path) -> None:
    summary = write_dataset(SETTINGS, tmp_path)
    on_disk = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert summary.files == on_disk
    assert summary.conversations == len(CONVERSATIONS)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["synthetic"] is True
    assert manifest["files"] == on_disk


def test_write_dataset_can_limit_platforms(tmp_path: Path) -> None:
    summary = write_dataset(SETTINGS, tmp_path, platforms=(Platform.IOS,))
    txt_dirs = {f.split("/")[1] for f in summary.files if f.startswith("txt/")}
    assert txt_dirs == {"ios-en"}


@pytest.mark.parametrize("locale", ["en", "pt-BR"])
def test_text_outputs_are_byte_identical_across_runs(tmp_path: Path, locale: str) -> None:
    settings = GeneratorSettings(seed=9, conversations=6, locale=locale)
    first = write_dataset(settings, tmp_path / "a")
    write_dataset(settings, tmp_path / "b")
    for relative in first.files:
        if relative.endswith(".xlsx"):
            continue
        assert (tmp_path / "a" / relative).read_bytes() == (tmp_path / "b" / relative).read_bytes()


def test_rewriting_removes_files_from_the_previous_dataset(tmp_path: Path) -> None:
    write_dataset(SETTINGS, tmp_path, platforms=(Platform.ANDROID,))
    (tmp_path / "notes.txt").write_text("user file", encoding="utf-8")
    summary = write_dataset(
        GeneratorSettings(seed=1, conversations=1), tmp_path, platforms=(Platform.IOS,)
    )
    on_disk = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert on_disk == sorted([*summary.files, "notes.txt"])
    assert not (tmp_path / "txt" / "android-en").exists()


def test_manifest_entries_outside_the_directory_are_never_deleted(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "manifest.json").write_text(
        json.dumps({"files": ["../outside.txt", str(outside)]}), encoding="utf-8"
    )
    write_dataset(SETTINGS, dataset)
    assert outside.read_text(encoding="utf-8") == "keep"


def test_unreadable_manifest_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("not json", encoding="utf-8")
    write_dataset(SETTINGS, tmp_path)
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))["synthetic"]
