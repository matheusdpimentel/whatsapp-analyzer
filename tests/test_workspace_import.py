"""`import --workspace`: identity, versions, history and all-or-nothing behavior."""

from pathlib import Path

import pytest
from openpyxl import Workbook

from helpers import count, data_of, database, exchange, new_workspace, row, run_cli, write_csv
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.synthetic.generator import GeneratorSettings
from whatsapp_analyzer.synthetic.writers import write_dataset


def import_csv(
    workspace: Path, path: Path, capsys: pytest.CaptureFixture[str], *extra: str
) -> tuple[int, dict[str, object]]:
    return run_cli(["import", str(path), "--workspace", str(workspace), *extra], capsys)


def test_first_import_stores_conversations_and_provenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    source = write_csv(tmp_path / "in.csv", [*exchange("alpha", 5), *exchange("beta", 6)])
    code, payload = import_csv(workspace, source, capsys)
    assert code == ExitCode.OK
    stored = data_of(payload)["workspace"]
    assert stored == {
        "import_id": 1,
        "conversations_new": 2,
        "conversations_changed": 0,
        "conversations_unchanged": 0,
        "versions_reactivated": 0,
        "messages_stored": 4,
    }
    connection = database(workspace)
    source_row = connection.execute("SELECT file_name, format, records FROM sources").fetchone()
    assert tuple(source_row) == ("in.csv", "csv", 4)
    versions = connection.execute(
        "SELECT message_count, is_group, first_at, last_at FROM conversation_versions ORDER BY id"
    ).fetchall()
    assert [tuple(v) for v in versions] == [
        (2, 0, "2026-01-05T10:00:00.000000", "2026-01-05T10:05:00.000000"),
        (2, 0, "2026-01-06T10:00:00.000000", "2026-01-06T10:05:00.000000"),
    ]
    connection.close()


def test_reimporting_identical_content_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    source = write_csv(tmp_path / "in.csv", [*exchange("alpha", 5), *exchange("beta", 6)])
    import_csv(workspace, source, capsys)
    connection = database(workspace)
    before = connection.execute("PRAGMA data_version").fetchone()[0]
    changes_before = [count(workspace, t) for t in ("imports", "sources", "messages")]

    code, payload = import_csv(workspace, source, capsys)
    assert code == ExitCode.OK
    stored = data_of(payload)["workspace"]
    assert isinstance(stored, dict)
    assert stored["import_id"] is None and stored["conversations_unchanged"] == 2
    assert [count(workspace, t) for t in ("imports", "sources", "messages")] == changes_before
    # data_version changes when another connection commits: nothing was committed.
    assert connection.execute("PRAGMA data_version").fetchone()[0] == before
    connection.close()


def test_changed_conversation_gets_a_new_version_and_history_is_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    first = write_csv(tmp_path / "v1.csv", [*exchange("alpha", 5), *exchange("beta", 6)])
    import_csv(workspace, first, capsys)
    later = [*exchange("alpha", 5), *exchange("alpha", 20), *exchange("beta", 6)]
    code, payload = import_csv(workspace, write_csv(tmp_path / "v2.csv", later), capsys)
    assert code == ExitCode.OK
    stored = data_of(payload)["workspace"]
    assert isinstance(stored, dict)
    assert (stored["conversations_changed"], stored["conversations_unchanged"]) == (1, 1)
    assert stored["messages_stored"] == 4

    connection = database(workspace)
    alpha = connection.execute(
        "SELECT id, current_version_id FROM conversations WHERE conversation_key = 'alpha'"
    ).fetchone()
    versions = connection.execute(
        "SELECT id, message_count FROM conversation_versions WHERE conversation_id = ? ORDER BY id",
        (alpha["id"],),
    ).fetchall()
    assert [v["message_count"] for v in versions] == [2, 4]
    assert alpha["current_version_id"] == versions[1]["id"]
    # The first version's messages are still there.
    assert (
        connection.execute(
            "SELECT count(*) FROM messages WHERE version_id = ?", (versions[0]["id"],)
        ).fetchone()[0]
        == 2
    )
    connection.close()


def test_returning_to_earlier_content_reuses_that_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    original = write_csv(tmp_path / "a.csv", exchange("alpha", 5))
    extended = write_csv(tmp_path / "b.csv", [*exchange("alpha", 5), *exchange("alpha", 9)])
    for path in (original, extended):
        import_csv(workspace, path, capsys)
    _, payload = import_csv(workspace, original, capsys)
    stored = data_of(payload)["workspace"]
    assert isinstance(stored, dict)
    assert stored["versions_reactivated"] == 1 and stored["messages_stored"] == 0
    assert count(workspace, "conversation_versions") == 2


def test_unrelated_replacement_is_refused_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    import_csv(
        workspace,
        write_csv(tmp_path / "a.csv", [*exchange("alpha", 5), *exchange("beta", 6)]),
        capsys,
    )
    # Same name, no message in common: probably a different chat called "alpha".
    other = write_csv(tmp_path / "b.csv", [*exchange("alpha", 15), *exchange("gamma", 7)])
    snapshot = [
        count(workspace, t)
        for t in ("imports", "conversations", "conversation_versions", "messages")
    ]

    code, payload = import_csv(workspace, other, capsys)
    assert code == ExitCode.VALIDATION_FAILED
    error = payload["error"]
    assert isinstance(error, dict) and error["code"] == "CONVERSATION_REPLACEMENT_CONFLICT"
    assert data_of(payload)["workspace"] is None
    # Not even the new, unrelated conversation "gamma" was stored.
    assert [
        count(workspace, t)
        for t in ("imports", "conversations", "conversation_versions", "messages")
    ] == snapshot

    code, payload = import_csv(workspace, other, capsys, "--replace-unrelated")
    assert code == ExitCode.OK
    stored = data_of(payload)["workspace"]
    assert isinstance(stored, dict)
    assert (stored["conversations_new"], stored["conversations_changed"]) == (1, 1)


def test_contact_renamed_between_exports_is_still_the_same_chat(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    import_csv(workspace, write_csv(tmp_path / "a.csv", exchange("alpha", 5)), capsys)
    renamed = [
        row(r[0], r[1], r[3], r[4], sender="New Name" if r[3] == "inbound" else None)
        for r in exchange("alpha", 5)
    ]
    code, _ = import_csv(
        workspace, write_csv(tmp_path / "b.csv", [*renamed, *exchange("alpha", 8)]), capsys
    )
    assert code == ExitCode.OK


def test_same_conversation_in_two_files_of_one_import_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    write_csv(tmp_path / "in" / "a.csv", exchange("alpha", 5))
    write_csv(tmp_path / "in" / "b.csv", exchange("alpha", 9))
    code, payload = import_csv(workspace, tmp_path / "in", capsys)
    assert code == ExitCode.VALIDATION_FAILED
    assert data_of(payload)["rejections"] == {"CONVERSATION_ID_CONFLICT": 1}
    assert count(workspace, "conversations") == 0


def test_formats_are_separate_namespaces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:

    workspace = new_workspace(tmp_path)
    rows = exchange("alpha", 5)
    import_csv(workspace, write_csv(tmp_path / "a.csv", rows), capsys)
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(
        [
            "conversation_id",
            "timestamp",
            "sender",
            "direction",
            "text",
            "message_type",
            "media_kind",
        ]
    )
    for item in rows:
        sheet.append(list(item))
    workbook.save(tmp_path / "a.xlsx")
    code, _ = import_csv(workspace, tmp_path / "a.xlsx", capsys)
    assert code == ExitCode.OK
    connection = database(workspace)
    keys = connection.execute(
        "SELECT source_format, conversation_key FROM conversations ORDER BY source_format"
    ).fetchall()
    assert [tuple(k) for k in keys] == [("csv", "alpha"), ("xlsx", "alpha")]
    connection.close()
    _, status = run_cli(["status", "--workspace", str(workspace)], capsys)
    conversations = data_of(status)["conversations"]
    assert isinstance(conversations, dict) and conversations["keys_in_several_formats"] == 1


def test_rejected_file_stores_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = new_workspace(tmp_path)
    write_csv(tmp_path / "in" / "good.csv", exchange("alpha", 5))
    (tmp_path / "in" / "bad.csv").write_text("not,a,canonical,file\n1,2,3,4\n", encoding="utf-8")
    code, _ = import_csv(workspace, tmp_path / "in", capsys)
    assert code == ExitCode.VALIDATION_FAILED
    assert count(workspace, "conversations") == 0 and count(workspace, "imports") == 0


def test_duplicates_are_preserved_in_the_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    rows = [*exchange("alpha", 5), exchange("alpha", 5)[1]]
    code, payload = import_csv(workspace, write_csv(tmp_path / "a.csv", rows), capsys)
    assert code == ExitCode.OK
    assert data_of(payload)["totals"]["duplicates"] == 1  # type: ignore[index]
    assert count(workspace, "messages") == 3


def test_drop_duplicates_cannot_be_combined_with_a_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    source = write_csv(tmp_path / "a.csv", exchange("alpha", 5))
    code, payload = import_csv(workspace, source, capsys, "--drop-duplicates")
    assert code == ExitCode.USAGE_ERROR
    assert payload["error"]["code"] == "INCOMPATIBLE_OPTIONS"  # type: ignore[index]


def test_import_into_missing_workspace_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = write_csv(tmp_path / "a.csv", exchange("alpha", 5))
    code, payload = import_csv(tmp_path / "nowhere", source, capsys)
    assert code == ExitCode.CONFIG_ERROR
    assert payload["error"]["code"] == "WORKSPACE_NOT_FOUND"  # type: ignore[index]


def test_group_detection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = new_workspace(tmp_path)
    rows = [
        row("team", "2026-01-05T10:00:00", "inbound", "a", sender="First"),
        row("team", "2026-01-05T10:01:00", "inbound", "b", sender="Second"),
        row("solo", "2026-01-05T10:00:00", "inbound", "a", sender="Only"),
        row("solo", "2026-01-05T10:01:00", "inbound", "b", sender=""),  # empty sender: not counted
    ]
    import_csv(workspace, write_csv(tmp_path / "a.csv", rows), capsys)
    connection = database(workspace)
    groups = dict(
        connection.execute(
            "SELECT c.conversation_key, v.is_group FROM conversations c "
            "JOIN conversation_versions v ON v.id = c.current_version_id"
        ).fetchall()
    )
    assert groups == {"team": 1, "solo": 0}
    connection.close()


def test_txt_imports_into_a_workspace(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:

    write_dataset(GeneratorSettings(seed=5, conversations=6), tmp_path / "data")
    workspace = new_workspace(tmp_path)
    code, payload = run_cli(
        [
            "import",
            str(tmp_path / "data" / "txt" / "ios-en"),
            "--owner",
            "Northwind Support",
            "--date-order",
            "mdy",
            "--workspace",
            str(workspace),
        ],
        capsys,
    )
    assert code == ExitCode.OK
    totals = data_of(payload)["totals"]
    assert isinstance(totals, dict)
    assert count(workspace, "messages") == totals["records_valid"]
    assert count(workspace, "conversations") == totals["conversations"]


def test_strict_import_with_invalid_records_stores_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    rows = [*exchange("alpha", 5), row("alpha", "not a date", "inbound")]
    source = write_csv(tmp_path / "a.csv", rows)
    out = tmp_path / "normalized.csv"
    code, payload = import_csv(workspace, source, capsys, "--strict", "--normalized-out", str(out))
    assert code == ExitCode.VALIDATION_FAILED
    assert payload["error"]["code"] == "INVALID_RECORDS"  # type: ignore[index]
    assert count(workspace, "messages") == 0 and count(workspace, "imports") == 0
    assert not out.exists()
    code, _ = import_csv(workspace, source, capsys)  # without --strict the valid rows are kept
    assert code == ExitCode.OK and count(workspace, "messages") == 2
