"""Metric runs against a real workspace database: caching, resumption, history and exports."""

import csv
import json
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from helpers import count, data_of, database, exchange, new_workspace, row, run_cli, write_csv
from whatsapp_analyzer.cli.app import main
from whatsapp_analyzer.cli.commands import metrics as metrics_command
from whatsapp_analyzer.config import DuplicatePolicy, ScoringSettings, Settings
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.metrics.reports import compare_runs, export_run, workspace_status
from whatsapp_analyzer.metrics.runner import ReferenceSource, RunMode, run_metrics
from whatsapp_analyzer.store.db import transaction
from whatsapp_analyzer.workspace import Workspace

SETTINGS = Settings()


def populated(tmp_path: Path, capsys: pytest.CaptureFixture[str], conversations: int = 6) -> Path:
    workspace = new_workspace(tmp_path)
    rows = []
    for index in range(conversations):
        rows += exchange(f"chat{index:02d}", 3 + index)
        rows += exchange(f"chat{index:02d}", 20 + index % 5, replies=index % 2 == 0)
    rows += [
        row("group", "2026-01-10T09:00:00", "inbound", "a", sender="First"),
        row("group", "2026-01-10T09:01:00", "inbound", "b", sender="Second"),
    ]
    code, _ = run_cli(
        ["import", str(write_csv(tmp_path / "in.csv", rows)), "--workspace", str(workspace)], capsys
    )
    assert code == ExitCode.OK
    return workspace


def snapshot_metrics(workspace: Path) -> list[tuple[object, ...]]:
    connection = database(workspace)
    try:
        return [
            tuple(r) for r in connection.execute("SELECT * FROM conversation_metrics ORDER BY 1, 2")
        ]
    finally:
        connection.close()


def test_first_run_then_reuse(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = populated(tmp_path, capsys)
    connection = database(workspace)
    first = run_metrics(connection, SETTINGS)
    assert first.mode is RunMode.NEW
    assert first.reference_source is ReferenceSource.DATASET_MAX
    assert first.reference_date == date(2026, 1, 24)  # latest message in the input
    assert first.conversations_total == 7 and first.metrics_computed == 7
    assert first.levels is not None and first.levels["NOT_COMPARABLE"] == 1

    again = run_metrics(connection, SETTINGS)
    assert (again.mode, again.run_id) == (RunMode.REUSED, first.run_id)
    assert again.metrics_computed == 0

    forced = run_metrics(connection, SETTINGS, force=True)
    assert forced.mode is RunMode.NEW and forced.run_id != first.run_id
    assert forced.metrics_computed == 0 and forced.metrics_reused == 7
    connection.close()


def test_scoring_change_does_not_touch_raw_metrics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys)
    connection = database(workspace)
    first = run_metrics(connection, SETTINGS)
    before = snapshot_metrics(workspace)

    changed = replace(SETTINGS, scoring=replace(SETTINGS.scoring, high_threshold=0.9))
    second = run_metrics(connection, changed)
    assert second.mode is RunMode.NEW and second.run_id != first.run_id
    assert second.metrics_hash == first.metrics_hash
    assert second.scoring_hash != first.scoring_hash
    assert (second.metrics_computed, second.metrics_reused) == (0, 7)
    assert snapshot_metrics(workspace) == before  # same rows, same computed_at
    connection.close()


def test_metrics_change_recomputes_and_keeps_old_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys)
    connection = database(workspace)
    first = run_metrics(connection, SETTINGS)
    changed = replace(
        SETTINGS, metrics=replace(SETTINGS.metrics, duplicates=DuplicatePolicy.COLLAPSE)
    )
    second = run_metrics(connection, changed)
    assert second.metrics_hash != first.metrics_hash
    assert second.metrics_computed == 7
    assert count(workspace, "conversation_metrics") == 14
    assert count(workspace, "metric_configs") == 2
    connection.close()


def test_reference_date_option_and_fixed_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys)
    connection = database(workspace)
    fixed = replace(SETTINGS, metrics=replace(SETTINGS.metrics, reference_date=date(2026, 2, 28)))
    from_config = run_metrics(connection, fixed)
    assert (from_config.reference_date, from_config.reference_source) == (
        date(2026, 2, 28),
        ReferenceSource.CONFIG,
    )
    from_option = run_metrics(connection, fixed, reference_override=date(2026, 1, 31))
    assert from_option.reference_source is ReferenceSource.OPTION
    assert from_option.metrics_hash != from_config.metrics_hash
    connection.close()


def test_only_the_changed_conversation_is_recomputed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    source = [*exchange("alpha", 5), *exchange("beta", 6), *exchange("gamma", 7)]
    run_cli(["import", str(write_csv(tmp_path / "a.csv", source)), "-w", str(workspace)], capsys)
    connection = database(workspace)
    fixed = replace(SETTINGS, metrics=replace(SETTINGS.metrics, reference_date=date(2026, 1, 31)))
    run_metrics(connection, fixed)

    updated = [*source, *exchange("beta", 12)]
    run_cli(["import", str(write_csv(tmp_path / "b.csv", updated)), "-w", str(workspace)], capsys)
    result = run_metrics(connection, fixed)
    assert result.mode is RunMode.NEW
    assert (result.metrics_computed, result.metrics_reused) == (1, 2)
    connection.close()


def test_history_is_marked_after_data_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    source = [*exchange("alpha", 5), *exchange("beta", 6)]
    run_cli(["import", str(write_csv(tmp_path / "a.csv", source)), "-w", str(workspace)], capsys)
    connection = database(workspace)
    fixed = replace(SETTINGS, metrics=replace(SETTINGS.metrics, reference_date=date(2026, 1, 31)))
    old_run = run_metrics(connection, fixed)
    run_cli(
        [
            "import",
            str(write_csv(tmp_path / "b.csv", [*source, *exchange("alpha", 9)])),
            "-w",
            str(workspace),
        ],
        capsys,
    )

    old = export_run(connection, old_run.run_id, tmp_path / "old.csv")
    assert old.metadata["current_data"] is False
    assert old.metadata["superseded_rows"] == 1
    rows = list(csv.DictReader((tmp_path / "old.csv").open(encoding="utf-8")))
    assert {r["conversation_key"]: r["data_version"] for r in rows} == {
        "alpha": "superseded",
        "beta": "current",
    }
    # The old run still has its scores; nothing was deleted or rewritten.
    assert old.rows == 2
    status = workspace_status(connection)
    runs = status["runs"]
    assert isinstance(runs, list) and runs[0]["current_data"] is False

    new_run = run_metrics(connection, fixed)
    new = export_run(connection, new_run.run_id, tmp_path / "new.csv")
    assert new.metadata["current_data"] is True and new.metadata["superseded_rows"] == 0
    connection.close()


class Interrupted(Exception):
    pass


def test_interrupted_run_resumes_without_duplicates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys)
    connection = database(workspace)

    def stop_after_first_batch(done: int, total: int) -> None:
        raise Interrupted

    with pytest.raises(Interrupted):
        run_metrics(connection, SETTINGS, progress=stop_after_first_batch, batch_size=3)
    run = connection.execute("SELECT status, conversations_scored FROM scoring_runs").fetchone()
    assert tuple(run) == ("incomplete", 3)

    resumed = run_metrics(connection, SETTINGS, batch_size=3)
    assert resumed.mode is RunMode.RESUMED
    assert count(workspace, "conversation_scores") == 7
    assert count(workspace, "conversation_metrics") == 7
    assert count(workspace, "scoring_runs") == 1

    reference = populated(tmp_path / "clean", capsys)
    clean_connection = database(reference)
    clean = run_metrics(clean_connection, SETTINGS)
    assert clean.levels == resumed.levels
    clean_connection.close()
    connection.close()


def test_run_left_running_by_a_crash_is_marked_incomplete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys)
    connection = database(workspace)
    first = run_metrics(connection, SETTINGS)
    with transaction(connection):
        connection.execute("UPDATE scoring_runs SET status = 'running'")
    other = run_metrics(connection, replace(SETTINGS, scoring=ScoringSettings(min_coverage=0.9)))
    statuses = dict(connection.execute("SELECT id, status FROM scoring_runs").fetchall())
    assert statuses[first.run_id] == "incomplete"
    assert statuses[other.run_id] == "complete"
    connection.close()


def test_export_during_a_run_is_a_consistent_partial_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys)
    writer = database(workspace)
    reader = database(workspace)
    exports: list[dict[str, object]] = []

    def export_while_running(done: int, total: int) -> None:
        if done < total:
            result = export_run(reader, None, tmp_path / f"partial-{done}.csv")
            exports.append(result.metadata)
            lines = (tmp_path / f"partial-{done}.csv").read_text(encoding="utf-8").splitlines()
            assert len(lines) - 1 == done

    run_metrics(writer, SETTINGS, progress=export_while_running, batch_size=2)
    assert [e["rows"] for e in exports] == [2, 4, 6]
    assert all(e["partial"] for e in exports)
    final = export_run(reader, None, tmp_path / "final.csv")
    assert final.metadata["partial"] is False and final.rows == 7
    writer.close()
    reader.close()


def test_export_reads_one_snapshot_even_if_data_is_committed_meanwhile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys)
    writer = database(workspace)
    reader = database(workspace)

    def interrupt(done: int, total: int) -> None:
        raise Interrupted

    with pytest.raises(Interrupted):
        run_metrics(writer, SETTINGS, progress=interrupt, batch_size=3)

    def concurrent_commit() -> None:
        run_metrics(writer, SETTINGS)  # finishes the run while the export is reading

    result = export_run(reader, None, tmp_path / "out.csv", on_snapshot=concurrent_commit)
    assert result.rows == 3
    assert result.metadata["partial"] is True
    run = result.metadata["run"]
    assert isinstance(run, dict) and run["status"] == "incomplete"
    assert export_run(reader, None, tmp_path / "after.csv").rows == 7
    writer.close()
    reader.close()


def test_results_are_deterministic_across_workspaces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outputs = []
    for name in ("one", "two"):
        workspace = populated(tmp_path / name, capsys)
        connection = database(workspace)
        run_metrics(connection, SETTINGS)
        export_run(connection, None, tmp_path / f"{name}.csv")
        outputs.append((tmp_path / f"{name}.csv").read_bytes())
        connection.close()
    assert outputs[0] == outputs[1]


def test_compare_runs_aggregates_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = populated(tmp_path, capsys)
    connection = database(workspace)
    first = run_metrics(connection, SETTINGS)
    strict = replace(
        SETTINGS, scoring=replace(SETTINGS.scoring, high_threshold=0.99, medium_threshold=0.98)
    )
    second = run_metrics(connection, strict)
    result = compare_runs(connection, first.run_id, second.run_id)
    assert result["compared"] == 7
    assert result["changed_settings"] == ["scoring.high_threshold", "scoring.medium_threshold"]
    transitions = result["transitions"]
    assert isinstance(transitions, dict)
    assert sum(sum(row.values()) for row in transitions.values()) == 7
    assert transitions["NOT_COMPARABLE"]["NOT_COMPARABLE"] == 1
    text = json.dumps(result)
    assert "chat0" not in text and "First" not in text
    connection.close()


def test_compare_excludes_conversations_whose_data_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    source = [*exchange("alpha", 5), *exchange("beta", 6)]
    run_cli(["import", str(write_csv(tmp_path / "a.csv", source)), "-w", str(workspace)], capsys)
    connection = database(workspace)
    first = run_metrics(connection, SETTINGS)
    run_cli(
        [
            "import",
            str(write_csv(tmp_path / "b.csv", [*source, *exchange("beta", 9)])),
            "-w",
            str(workspace),
        ],
        capsys,
    )
    second = run_metrics(connection, SETTINGS)
    result = compare_runs(connection, first.run_id, second.run_id)
    assert (result["compared"], result["different_data_version"]) == (1, 1)
    assert result["changed_settings"] == ["metrics.reference_date"]
    connection.close()


def test_export_escapes_formulas(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = new_workspace(tmp_path)
    run_cli(
        ["import", str(write_csv(tmp_path / "a.csv", exchange("=cmd", 5))), "-w", str(workspace)],
        capsys,
    )
    connection = database(workspace)
    run_metrics(connection, SETTINGS)
    export_run(connection, None, tmp_path / "out.csv")
    rows = list(csv.DictReader((tmp_path / "out.csv").open(encoding="utf-8")))
    assert rows[0]["conversation_key"] == "'=cmd"
    connection.close()


def test_status_contains_no_conversation_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys)
    connection = database(workspace)
    run_metrics(connection, SETTINGS)
    status = workspace_status(connection)
    text = json.dumps(status)
    for forbidden in ("chat0", "First", "Second", "Hi there", "contact"):
        assert forbidden not in text
    connection.close()


def test_status_and_export_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys)
    code, payload = run_cli(["metrics", "run", "-w", str(workspace)], capsys)
    assert code == ExitCode.OK and payload["command"] == "metrics run"
    run = data_of(payload)
    assert run["mode"] == "new" and run["reference_date"] == "2026-01-24"

    code, payload = run_cli(
        ["metrics", "run", "-w", str(workspace), "--reference-date", "2026-02-01"], capsys
    )
    assert data_of(payload)["reference_date_source"] == "option"

    code, payload = run_cli(["metrics", "compare", "1", "2", "-w", str(workspace)], capsys)
    assert code == ExitCode.OK and data_of(payload)["changed_settings"] == [
        "metrics.reference_date"
    ]

    code, payload = run_cli(["status", "-w", str(workspace)], capsys)
    assert code == ExitCode.OK and data_of(payload)["runs_total"] == 2

    out = tmp_path / "scores.csv"
    code, payload = run_cli(
        ["export", "-w", str(workspace), "--run", "1", "--out", str(out)], capsys
    )
    assert code == ExitCode.OK and data_of(payload)["rows"] == 7
    code, payload = run_cli(["export", "-w", str(workspace), "--out", str(out)], capsys)
    assert code == ExitCode.USAGE_ERROR and payload["error"]["code"] == "OUTPUT_EXISTS"  # type: ignore[index]
    code, payload = run_cli(
        ["export", "-w", str(workspace), "--run", "9", "--out", str(tmp_path / "x.csv")], capsys
    )
    assert code == ExitCode.USAGE_ERROR and payload["error"]["code"] == "RUN_NOT_FOUND"  # type: ignore[index]


def test_cli_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty = new_workspace(tmp_path)
    code, payload = run_cli(["metrics", "run", "-w", str(empty)], capsys)
    assert (code, payload["error"]["code"]) == (ExitCode.USAGE_ERROR, "NO_CONVERSATIONS")  # type: ignore[index]
    code, payload = run_cli(["export", "-w", str(empty), "--out", str(tmp_path / "x.csv")], capsys)
    assert payload["error"]["code"] == "NO_RUNS"  # type: ignore[index]

    (empty / "analyzer.toml").write_text("[scoring]\nmin_coverage = 2\n", encoding="utf-8")
    code, payload = run_cli(["metrics", "run", "-w", str(empty)], capsys)
    assert (code, payload["error"]["code"]) == (ExitCode.CONFIG_ERROR, "CONFIG_INVALID")  # type: ignore[index]

    code, payload = run_cli(["status", "-w", str(tmp_path / "missing")], capsys)
    assert (code, payload["error"]["code"]) == (ExitCode.CONFIG_ERROR, "WORKSPACE_NOT_FOUND")  # type: ignore[index]

    (empty / "analyzer.toml").write_text("", encoding="utf-8")
    code, payload = run_cli(
        ["metrics", "run", "-w", str(empty), "--reference-date", "31/01/2026"], capsys
    )
    assert (code, payload["error"]["code"]) == (ExitCode.USAGE_ERROR, "CONFIG_INVALID")  # type: ignore[index]


def test_busy_workspace(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:

    workspace = populated(tmp_path, capsys)
    with Workspace(workspace).lock():
        code, payload = run_cli(["metrics", "run", "-w", str(workspace)], capsys)
        assert (code, payload["error"]["code"]) == (ExitCode.USAGE_ERROR, "WORKSPACE_BUSY")  # type: ignore[index]
        code, _ = run_cli(["status", "-w", str(workspace)], capsys)  # readers are not blocked
        assert code == ExitCode.OK


def test_init_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = run_cli(["init", str(tmp_path / "ws")], capsys)
    assert code == ExitCode.OK and data_of(payload)["schema_version"] == 1
    code, payload = run_cli(["init", str(tmp_path / "ws")], capsys)
    assert (code, payload["error"]["code"]) == (ExitCode.USAGE_ERROR, "WORKSPACE_EXISTS")  # type: ignore[index]


def test_human_output_in_portuguese(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:

    workspace = populated(tmp_path, capsys)
    assert main(["--lang", "pt-BR", "metrics", "run", "-w", str(workspace)]) == ExitCode.OK
    output = capsys.readouterr().out
    assert "Data de referência: 2026-01-24" in output
    assert main(["--lang", "pt-BR", "status", "-w", str(workspace)]) == ExitCode.OK
    assert "Runs recentes" in capsys.readouterr().out


def test_dataset_max_ignores_system_messages_and_superseded_versions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    first = [
        *exchange("alpha", 5),
        *exchange("alpha", 28),
        row("beta", "2026-01-10T10:00:00", "inbound", "hi"),
        row(
            "beta", "2026-03-01T10:00:00", "", "settings changed", sender="", message_type="system"
        ),
    ]
    run_cli(["import", str(write_csv(tmp_path / "a.csv", first)), "-w", str(workspace)], capsys)
    connection = database(workspace)
    assert run_metrics(connection, SETTINGS).reference_date == date(2026, 1, 28)

    # alpha is re-exported without its latest exchange: the stored later version is superseded.
    shorter = [*exchange("alpha", 5), *first[4:]]
    run_cli(["import", str(write_csv(tmp_path / "b.csv", shorter)), "-w", str(workspace)], capsys)
    assert run_metrics(connection, SETTINGS).reference_date == date(2026, 1, 10)
    connection.close()


def test_default_export_is_the_run_used_last(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys)
    connection = database(workspace)
    config_a = SETTINGS
    config_b = replace(SETTINGS, scoring=replace(SETTINGS.scoring, high_threshold=0.9))
    run_a = run_metrics(connection, config_a)
    run_metrics(connection, config_b)
    reused = run_metrics(connection, config_a)
    assert (reused.mode, reused.run_id) == (RunMode.REUSED, run_a.run_id)
    result = export_run(connection, None, tmp_path / "out.csv")
    run = result.metadata["run"]
    assert isinstance(run, dict) and run["id"] == run_a.run_id
    connection.close()


def test_every_csv_row_states_the_run_status_and_data_currency(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    run_cli(
        ["import", str(write_csv(tmp_path / "a.csv", exchange("alpha", 5))), "-w", str(workspace)],
        capsys,
    )
    connection = database(workspace)
    old = run_metrics(connection, SETTINGS)
    # A new conversation moves dataset_max: unchanged "alpha" stays "current" data, but run 1
    # was computed with an older reference date and no longer matches the workspace.
    run_cli(
        ["import", str(write_csv(tmp_path / "b.csv", exchange("beta", 20))), "-w", str(workspace)],
        capsys,
    )
    export_run(connection, old.run_id, tmp_path / "old.csv")
    rows = list(csv.DictReader((tmp_path / "old.csv").open(encoding="utf-8")))
    assert [(r["data_version"], r["run_status"], r["run_current_data"]) for r in rows] == [
        ("current", "complete", "false")
    ]
    connection.close()


def test_database_errors_map_to_the_output_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = populated(tmp_path, capsys)

    def disk_full(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(metrics_command, "run_metrics", disk_full)
    code, payload = run_cli(["metrics", "run", "-w", str(workspace)], capsys)
    assert (code, payload["error"]["code"]) == (ExitCode.OUTPUT_ERROR, "DATABASE_ERROR")  # type: ignore[index]
    assert payload["command"] == "metrics run"


def test_init_into_a_non_empty_directory_via_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "existing.txt").write_text("x", encoding="utf-8")
    code, payload = run_cli(["init", str(tmp_path)], capsys)
    assert (code, payload["error"]["code"]) == (ExitCode.USAGE_ERROR, "DIRECTORY_NOT_EMPTY")  # type: ignore[index]
