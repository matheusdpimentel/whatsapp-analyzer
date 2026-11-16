"""Milestone 8: calibration against human labels (offline; results seeded by hand).

The runs are inserted directly with labels chosen here, so every expected number below was
worked out from this table and not from the code under test:

    conversation i (0..39) has human label LEVELS[i % 3] (LOW, MEDIUM, HIGH, LOW, ...)
    primary model is wrong (next level up, HIGH wraps to LOW) when i % 5 == 0  -> 8 items
    reviewer is wrong the same way when i % 4 == 1                             -> 10 items
    both wrong (and equal): i = 5 and 25; so exactly one wrong: 6 + 8 = 14 items
    => primary accuracy 32/40, reviewer 30/40, they agree on 26 (24 right, 2 wrong)
"""

import csv
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from helpers import database, new_workspace, row, run_cli, write_csv
from whatsapp_analyzer.calibration.suggest import improvement_holds
from whatsapp_analyzer.cli.app import main
from whatsapp_analyzer.config import ConfigError, parse_settings
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.fileio import atomic_write_files

LEVELS = ("LOW", "MEDIUM", "HIGH")
ESCAPE = chr(27) + "[31m"
BIDI = chr(0x202E)
N = 40


def truth(i: int) -> str:
    return LEVELS[i % 3]


def wrong(label: str) -> str:
    return LEVELS[(LEVELS.index(label) + 1) % 3]


def primary_label(i: int) -> str:
    return wrong(truth(i)) if i % 5 == 0 else truth(i)


def reviewer_label(i: int) -> str:
    return wrong(truth(i)) if i % 4 == 1 else truth(i)


def insert_run(
    db: sqlite3.Connection, model: str, labels: dict[int, str | None], context: str = "ctx"
) -> int:
    run_id = db.execute(
        "INSERT INTO llm_runs (status, run_key, scope_fingerprint, provider, model, prompt_id, "
        "prompt_version, template_hash, context_config_hash, generation_json, limits_json, "
        "jobs_total, tool_version, started_at, last_used_seq) "
        "VALUES ('completed', ?, 'scope', 'fake', ?, 'p', '1', 't', 'c', '{}', '{}', ?, 'x', "
        "'2026-01-01T00:00:00+00:00', ?) RETURNING id",
        (f"key-{model}", model, len(labels), 100 + len(model)),
    ).fetchone()[0]
    for version, label in labels.items():
        db.execute(
            "INSERT INTO llm_jobs (run_id, version_id, status, request_id, context_hash, "
            "prompt_hash, label, reasons_json) VALUES (?, ?, ?, ?, ?, ?, ?, '[]')",
            (
                run_id,
                version,
                "succeeded" if label else "failed",
                f"req-{model}-{version}",
                context,
                f"prompt-{model}",
                label,
            ),
        )
    return int(run_id)


@pytest.fixture
def workspace(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    root = new_workspace(tmp_path)
    rows = []
    for i in range(N):
        key = f"c{i:02d}"
        rows += [
            row(key, "2026-01-05T10:00:00", "outbound", "Hello"),
            row(key, "2026-01-05T10:05:00", "inbound", f"reply {i}", sender="Contact"),
        ]
    assert (
        run_cli(["import", str(write_csv(tmp_path / "in.csv", rows)), "-w", str(root)], capsys)[0]
        == 0
    )
    db = database(root)
    versions = {
        r[0]: r[1]
        for r in db.execute(
            "SELECT c.conversation_key, v.id FROM conversation_versions v "
            "JOIN conversations c ON c.id = v.conversation_id"
        )
    }
    by_index = {i: versions[f"c{i:02d}"] for i in range(N)}
    primary = insert_run(db, "fake-classifier", {by_index[i]: primary_label(i) for i in range(N)})
    reviewer = insert_run(db, "fake-reviewer", {by_index[i]: reviewer_label(i) for i in range(N)})
    db.execute(
        "INSERT INTO llm_reviews (primary_run_id, review_run_id, mode, selection_key, "
        "criteria_json, policy, selected, created_at, used_seq) VALUES (?, ?, 'dual', 'k', "
        '\'{"criteria": [], "scoring_run": null, "keys": []}\', \'human_review\', ?, '
        "'2026-01-01T00:00:00+00:00', 1)",
        (primary, reviewer, N),
    )
    db.close()
    return {
        "root": root,
        "tmp": tmp_path,
        "versions": by_index,
        "primary": primary,
        "reviewer": reviewer,
    }


def cli(
    ws: dict[str, Any], capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, dict[str, Any]]:
    head, tail = list(args[:2]), list(args[2:])
    return run_cli([*head, "-w", str(ws["root"]), *tail], capsys)


def sample(
    ws: dict[str, Any], capsys: pytest.CaptureFixture[str], name: str, size: int = N, *extra: str
) -> tuple[int, dict[str, Any]]:
    out = ws["tmp"] / f"{name}.csv"
    return cli(
        ws,
        capsys,
        "calibrate",
        "sample",
        "--set",
        name,
        "--size",
        str(size),
        "--out",
        str(out),
        "--overwrite",
        *extra,
    )


def read_sheet(ws: dict[str, Any], name: str) -> list[dict[str, str]]:
    with (ws["tmp"] / f"{name}.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def label_sheet(
    ws: dict[str, Any], name: str, labeler: Any = truth, only: int | None = None
) -> Path:
    rows = read_sheet(ws, name)
    path: Path = ws["tmp"] / f"{name}-filled.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        for r in rows:
            index = int(r["conversation_key"][1:])
            r["human_label"] = labeler(index) if only is None or index < only else ""
            writer.writerow(r)
    return path


def import_all(
    ws: dict[str, Any], capsys: pytest.CaptureFixture[str], name: str, **kwargs: Any
) -> dict[str, Any]:
    path = label_sheet(ws, name, **kwargs)
    code, payload = cli(ws, capsys, "calibrate", "import", "--set", name, "--labels", str(path))
    assert code == 0, payload
    return payload["data"]  # type: ignore[no-any-return]


def report(
    ws: dict[str, Any], capsys: pytest.CaptureFixture[str], name: str, *extra: str
) -> tuple[int, dict[str, Any]]:
    out = ws["tmp"] / f"{name}-report.csv"
    return cli(
        ws, capsys, "calibrate", "report", "--set", name, "--out", str(out), "--overwrite", *extra
    )


def config(ws: dict[str, Any], text: str) -> list[str]:
    path = ws["tmp"] / "cfg.toml"
    path.write_text(text, encoding="utf-8")
    return ["--config", str(path)]


# Sampling and the sheet -------------------------------------------------------------------


def test_sample_writes_ids_only_and_is_reproducible(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = sample(workspace, capsys, "a", 20, "--seed", "7")
    assert code == 0, payload
    assert (
        payload["data"]["sampling"] == "representative" and payload["data"]["generalizable"] is True
    )
    first = read_sheet(workspace, "a")
    assert len(first) == 20 and list(first[0]) == [
        "set",
        "conversation_key",
        "version_id",
        "data_version",
        "human_label",
    ]
    assert {r["human_label"] for r in first} == {""}
    text = (workspace["tmp"] / "a.csv").read_text(encoding="utf-8")
    assert "reply" not in text and "Hello" not in text  # no message text
    assert sample(workspace, capsys, "b", 20, "--seed", "7")[0] == 0
    assert [r["version_id"] for r in read_sheet(workspace, "b")] == [r["version_id"] for r in first]
    assert sample(workspace, capsys, "c", 20, "--seed", "8")[0] == 0
    assert {r["version_id"] for r in read_sheet(workspace, "c")} != {r["version_id"] for r in first}
    meta = json.loads((workspace["tmp"] / "a.csv.meta.json").read_text(encoding="utf-8"))
    assert meta["content_included"] is False and meta["population"] == N


def test_sample_refuses_duplicates_bad_sizes_and_bad_names(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert sample(workspace, capsys, "a", 10)[0] == 0
    code, payload = sample(workspace, capsys, "a", 10)
    assert code == ExitCode.USAGE_ERROR and payload["error"]["code"] == "SET_EXISTS"
    code, payload = sample(workspace, capsys, "big", N + 1)
    assert payload["error"]["code"] == "POPULATION_TOO_SMALL"
    assert sample(workspace, capsys, "bad name!", 5)[1]["error"]["code"] == "INVALID_SET_NAME"
    assert (
        sample(workspace, capsys, "x", 5, "--strategy", "random")[1]["error"]["code"]
        == "INVALID_STRATEGY"
    )


def test_prioritized_sampling_takes_divergences_first(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = sample(workspace, capsys, "p", 20, "--strategy", "prioritized")
    assert code == 0 and payload["data"]["generalizable"] is False
    db = database(workspace["root"])
    strata = dict(
        db.execute("SELECT stratum, count(*) FROM calibration_items GROUP BY stratum").fetchall()
    )
    assert strata == {"divergence": 14, "other": 6}  # 14 versions where the two models differ
    diverging = {
        workspace["versions"][i] for i in range(N) if primary_label(i) != reviewer_label(i)
    }
    chosen = {
        r[0]
        for r in db.execute("SELECT version_id FROM calibration_items WHERE stratum = 'divergence'")
    }
    assert chosen == diverging
    code, payload = sample(workspace, capsys, "p10", 10, "--strategy", "prioritized")
    assert dict(
        database(workspace["root"])
        .execute(
            "SELECT stratum, count(*) FROM calibration_items i "
            "JOIN calibration_sets s ON s.id = i.set_id WHERE s.name = 'p10' GROUP BY stratum"
        )
        .fetchall()
    ) == {"divergence": 10}


# Importing human labels -------------------------------------------------------------------


def test_import_is_all_or_nothing_and_labels_are_immutable(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    sample(workspace, capsys, "s")
    bad = label_sheet(workspace, "s", labeler=lambda i: "high" if i == 3 else truth(i))
    code, payload = cli(
        workspace, capsys, "calibrate", "import", "--set", "s", "--labels", str(bad)
    )
    assert code == ExitCode.VALIDATION_FAILED and payload["error"]["code"] == "LABELS_INVALID"
    assert payload["error"]["details"]["problems"][0]["reason"] == "invalid_label"
    assert (
        database(workspace["root"]).execute("SELECT count(*) FROM calibration_labels").fetchone()[0]
        == 0
    )
    data = import_all(workspace, capsys, "s", only=35)  # the last five stay blank
    assert (data["new"], data["pending_in_file"], data["usable"]) == (35, 5, 35)
    data = import_all(workspace, capsys, "s")  # the rest, same labels for the first 35
    assert (data["new"], data["unchanged"], data["usable"]) == (5, 35, 40)
    changed = label_sheet(
        workspace, "s", labeler=lambda i: "LOW" if i == 1 else truth(i)
    )  # item 1 is MEDIUM
    code, payload = cli(
        workspace, capsys, "calibrate", "import", "--set", "s", "--labels", str(changed)
    )
    assert payload["error"]["code"] == "LABELS_INVALID"
    assert payload["error"]["details"]["problems"][0]["reason"] == "label_conflict"
    db = database(workspace["root"])
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE calibration_labels SET label = 'LOW'")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM calibration_items")


def test_import_rejects_edited_ids_duplicates_and_other_sets(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    sample(workspace, capsys, "s", 10)
    rows = read_sheet(workspace, "s")
    cases: dict[str, Callable[[list[dict[str, str]]], object]] = {
        "key_mismatch": lambda r: r[0].update(conversation_key="zzz"),
        "unknown_version_id": lambda r: r[0].update(version_id="999999"),
        "other_set": lambda r: r[0].update(set="other"),
        "duplicate_row": lambda r: r.append(dict(r[0])),
    }
    for reason, mutate in cases.items():
        edited = [dict(r, human_label="LOW") for r in rows]
        mutate(edited)
        path = workspace["tmp"] / "edited.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(edited)
        code, payload = cli(
            workspace, capsys, "calibrate", "import", "--set", "s", "--labels", str(path)
        )
        assert code == ExitCode.VALIDATION_FAILED, reason
        assert payload["error"]["details"]["problems"][0]["reason"] == reason
    assert (
        database(workspace["root"]).execute("SELECT count(*) FROM calibration_labels").fetchone()[0]
        == 0
    )


# Report -------------------------------------------------------------------------------------


def test_report_is_gated_by_the_minimum_number_of_labels(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    sample(workspace, capsys, "s")
    import_all(workspace, capsys, "s", only=10)
    code, payload = report(workspace, capsys, "s")
    assert code == ExitCode.CALIBRATION_REQUIRED and int(code) == 10
    assert payload["error"]["code"] == "CALIBRATION_REQUIRED"
    assert payload["error"]["details"] == {"command": "report", "have": 10, "need": 30}
    assert not (workspace["tmp"] / "s-report.csv").exists()
    lowered = config(workspace, "[calibration]\nmin_labels_report = 10\n")
    assert report(workspace, capsys, "s", *lowered)[0] == 0


def test_report_numbers_for_a_representative_set(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    sample(workspace, capsys, "s")
    import_all(workspace, capsys, "s")
    code, payload = report(workspace, capsys, "s")
    assert code == 0, payload
    data = payload["data"]
    raters = {r["rater"]: r for r in data["raters"]}
    primary = raters["primary:fake/fake-classifier"]
    assert (primary["compared"], primary["accuracy"]) == (40, 0.8)
    assert (primary["off_by_one"], primary["off_by_two"]) == (5, 3)  # HIGH wrapped to LOW: 3 items
    assert primary["ci95"] is not None and primary["ci95"][0] < 0.8 < primary["ci95"][1]
    assert raters["reviewer:fake/fake-reviewer"]["accuracy"] == 0.75
    assert data["model_pair"] == {
        "items_both_labeled": 40,
        "agreement": 0.65,
        "weighted_kappa": data["model_pair"]["weighted_kappa"],  # checked in test_calibration_stats
        "when_they_disagree_human_sided_with": {"primary": 8, "reviewer": 6, "neither": 0},
    }
    agreement = raters["consolidated:require_agreement"]
    assert (agreement["compared"], agreement["abstained"], agreement["needs_human_review"]) == (
        26,
        14,
        14,
    )
    assert agreement["accuracy"] == round(24 / 26, 4)
    assert raters["consolidated:prefer_primary"]["accuracy"] == 0.8
    assert raters["consolidated:prefer_reviewer"]["accuracy"] == 0.75
    assert (
        data["generalizable"] is True
        and "prioritized_sample_not_generalizable" not in data["caveats"]
    )
    assert "pending_labels" not in data["caveats"]
    # absent or abstaining raters are counted, never counted as wrong
    assert raters["primary:fake/fake-classifier"]["abstained"] == 0
    meta = json.loads((workspace["tmp"] / "s-report.csv.meta.json").read_text(encoding="utf-8"))
    assert meta["content_included"] is False
    assert "reply" not in (workspace["tmp"] / "s-report.csv").read_text(encoding="utf-8")


def test_report_does_not_present_a_prioritized_sample_as_general(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    sample(workspace, capsys, "p", 40, "--strategy", "prioritized")
    import_all(workspace, capsys, "p")
    code, payload = report(workspace, capsys, "p")
    assert code == 0
    data = payload["data"]
    assert (
        data["generalizable"] is False and "prioritized_sample_not_generalizable" in data["caveats"]
    )
    assert all(r["ci95"] is None for r in data["raters"])
    with (workspace["tmp"] / "p-report.csv").open(encoding="utf-8", newline="") as handle:
        assert {r["generalizable"] for r in csv.DictReader(handle)} == {"false"}


def test_report_flags_small_samples_unclear_labels_and_superseded_versions(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    sample(workspace, capsys, "s")
    import_all(workspace, capsys, "s", labeler=lambda i: "UNCLEAR" if i == 0 else truth(i))
    data = report(workspace, capsys, "s")[1]["data"]
    assert data["human_labeled"] == 39  # UNCLEAR is excluded from agreement
    assert {"small_sample", "unclear_labels_excluded"} <= set(data["caveats"])
    assert next(r for r in data["raters"] if r["rater"].startswith("primary"))["compared"] == 39


def test_report_evaluates_extra_runs_and_lists_absent_items(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    sample(workspace, capsys, "s")
    import_all(workspace, capsys, "s")
    db = database(workspace["root"])
    partial: dict[int, str | None] = {
        workspace["versions"][i]: truth(i) for i in range(30)
    }  # 10 versions never planned
    extra = insert_run(db, "other-model", partial)
    db.close()
    data = report(workspace, capsys, "s", "--run", str(extra))[1]["data"]
    rater = next(r for r in data["raters"] if r["rater"].startswith(f"run:{extra}"))
    assert (rater["compared"], rater["absent"], rater["accuracy"]) == (30, 10, 1.0)


# Compare ------------------------------------------------------------------------------------


def test_compare_pairs_same_versions_and_reports_what_it_leaves_out(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    sample(workspace, capsys, "s")
    import_all(workspace, capsys, "s")
    v = workspace["versions"]
    db = database(workspace["root"])
    labels: dict[int, str | None] = {v[i]: truth(i) for i in range(2, N)}  # items 0, 1 absent
    second = insert_run(db, "second-model", labels)
    db.execute(
        "UPDATE llm_jobs SET context_hash = 'other' WHERE run_id = ? AND version_id IN (?, ?, ?)",
        (second, v[10], v[11], v[12]),
    )
    db.execute(
        "UPDATE llm_jobs SET status = 'failed', label = NULL WHERE run_id = ? AND version_id = ?",
        (second, v[13]),
    )
    db.close()
    out = workspace["tmp"] / "cmp.csv"
    code, payload = cli(
        workspace,
        capsys,
        "calibrate",
        "compare",
        "--set",
        "s",
        "--run",
        str(workspace["primary"]),
        "--run",
        str(second),
        "--out",
        str(out),
    )
    assert code == 0, payload
    data = payload["data"]
    assert data["excluded_by_reason"] == {
        "absent_in_b": 2,
        "context_mismatch": 3,
        "no_ordinal_label": 1,
    }
    assert data["paired"] == 34 and "context_mismatch" in data["caveats"]
    a, b = data["runs"]
    assert b["accuracy_on_paired"] == 1.0 and a["accuracy_on_paired"] < 1.0
    assert a["recorded_cost"] is None or isinstance(a["recorded_cost"], str)
    # the primary is wrong on 5, 15, 20, 25, 30, 35 among the 34 paired items; run B never is
    assert a["accuracy_on_paired"] == pytest.approx(28 / 34, abs=1e-4)
    assert data["discordant"] == {"only_a_correct": 0, "only_b_correct": 6}
    with out.open(encoding="utf-8", newline="") as handle:
        pairing = [r["pairing"] for r in csv.DictReader(handle)]
    assert pairing.count("paired") == 34 and pairing.count("absent_in_b") == 2


def test_compare_needs_two_different_runs(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    sample(workspace, capsys, "s")
    import_all(workspace, capsys, "s")
    run = str(workspace["primary"])
    assert (
        cli(workspace, capsys, "calibrate", "compare", "--set", "s", "--run", run, "--run", run)[1][
            "error"
        ]["code"]
        == "COMPARE_SAME_RUN"
    )
    assert (
        cli(workspace, capsys, "calibrate", "compare", "--set", "s", "--run", run)[1]["error"][
            "code"
        ]
        == "INVALID_OPTION"
    )
    assert (
        cli(workspace, capsys, "calibrate", "compare", "--set", "s", "--run", run, "--run", "999")[
            1
        ]["error"]["code"]
        == "RUN_NOT_FOUND"
    )


# Disagreements ------------------------------------------------------------------------------


def test_disagreements_are_listed_widest_gap_first(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    out = workspace["tmp"] / "d.csv"
    code, payload = cli(workspace, capsys, "calibrate", "disagreements", "--out", str(out))
    assert code == 0, payload
    assert payload["data"]["diverging"] == 14 and payload["data"]["by_kind"] == {
        "model_disagreement": 14
    }
    with out.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    distances = [int(r["model_distance"]) for r in rows]
    assert distances == [2, 2, 2, 2] + [1] * 10  # HIGH vs LOW cases first
    assert {r["in_calibration_set"] for r in rows} == {""}
    sample(workspace, capsys, "s", 10, "--strategy", "prioritized")  # 10 of the 14 divergences
    import_all(workspace, capsys, "s")
    code, payload = cli(
        workspace,
        capsys,
        "calibrate",
        "disagreements",
        "--set",
        "s",
        "--out",
        str(out),
        "--overwrite",
    )
    with out.open(encoding="utf-8", newline="") as handle:
        marked = list(csv.DictReader(handle))
    assert sum(r["in_calibration_set"] == "true" for r in marked) == 10
    assert all(r["human_label"] for r in marked if r["in_calibration_set"] == "true")


# Show ---------------------------------------------------------------------------------------


def test_show_json_has_no_message_text_and_the_terminal_view_is_sanitized(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    sample(workspace, capsys, "s", 5)
    first = read_sheet(workspace, "s")[0]
    db = database(workspace["root"])
    db.execute(
        "UPDATE messages SET text = ? WHERE version_id = ?",
        (ESCAPE + "red" + BIDI + "txt", first["version_id"]),
    )
    db.close()
    code, payload = cli(
        workspace, capsys, "calibrate", "show", "--set", "s", "--version", first["version_id"]
    )
    assert code == 0, payload
    data = payload["data"]
    assert data["conversation_key"] == first["conversation_key"] and data["total_messages"] == 2
    assert data["content_included"] is False
    assert len(data["messages"]) == 2 and all(
        set(m) == {"seq", "timestamp", "direction", "type"} for m in data["messages"]
    )
    dumped = json.dumps(payload)
    assert "red" not in dumped and "Hello" not in dumped and "Contact" not in dumped
    assert not {"primary", "reviewer", "label"} & set(data) and "HIGH" not in dumped
    # the person labeling still reads the conversation on the terminal, sanitized
    assert (
        main(
            [
                "calibrate",
                "show",
                "-w",
                str(workspace["root"]),
                "--set",
                "s",
                "--version",
                first["version_id"],
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "red" in printed and "Contact" in printed
    assert ESCAPE not in printed and BIDI not in printed
    keys = {r["conversation_key"] for r in read_sheet(workspace, "s")}
    outside = next(i for i in range(N) if f"c{i:02d}" not in keys)
    code, payload = cli(
        workspace, capsys, "calibrate", "show", "--set", "s", "--key", f"c{outside:02d}"
    )
    assert payload["error"]["code"] == "ITEM_NOT_IN_SET"
    assert (
        cli(workspace, capsys, "calibrate", "show", "--set", "s")[1]["error"]["code"]
        == "SHOW_TARGET"
    )


# Suggest ------------------------------------------------------------------------------------


def link_scoring_run(ws: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> int:
    assert run_cli(["metrics", "run", "-w", str(ws["root"])], capsys)[0] == 0
    db = database(ws["root"])
    scoring = db.execute("SELECT id FROM scoring_runs WHERE status = 'complete'").fetchone()[0]
    db.execute(
        "UPDATE llm_reviews SET criteria_json = ?",
        (json.dumps({"criteria": [], "scoring_run": scoring, "keys": []}),),
    )
    db.close()
    return int(scoring)


def test_suggest_needs_fifty_labels_unless_configured(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    sample(workspace, capsys, "s")
    import_all(workspace, capsys, "s")
    code, payload = cli(workspace, capsys, "calibrate", "suggest", "--set", "s")
    assert code == ExitCode.CALIBRATION_REQUIRED
    assert payload["error"]["details"] == {"command": "suggest", "have": 40, "need": 50}
    lowered = config(workspace, "[calibration]\nmin_labels_suggest = 30\n")
    assert cli(workspace, capsys, "calibrate", "suggest", "--set", "s", *lowered)[0] == 0


def test_suggest_is_exploratory_separates_validation_and_applies_nothing(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    scoring = link_scoring_run(workspace, capsys)
    sample(workspace, capsys, "s")
    import_all(workspace, capsys, "s")
    config_path = workspace["root"] / "analyzer.toml"
    before = config_path.read_bytes()
    lowered = config(workspace, "[calibration]\nmin_labels_suggest = 30\n")
    out = workspace["tmp"] / "sug.csv"
    code, payload = cli(
        workspace, capsys, "calibrate", "suggest", "--set", "s", "--out", str(out), *lowered
    )
    assert code == 0, payload
    data = payload["data"]
    assert data["exploratory"] is True and data["applied"] is False
    assert (
        data["development_items"] + data["validation_items"] == 40 and data["validation_items"] > 0
    )
    assert {"exploratory_suggestions", "small_sample"} <= set(data["caveats"])
    for part in ("development", "validation"):  # every policy is measured on each part
        assert {m["items"] for m in data["policies"][part].values()} == {data[f"{part}_items"]}
    # without weights, policies are not ranked: only a frontier of non-dominated ones is given
    assert set(data["frontier_in_both_parts"]) <= {
        "require_agreement",
        "prefer_primary",
        "prefer_reviewer",
        "human_review",
    }
    assert (
        "human_review" in data["frontier"]["development"]
        or "require_agreement" in data["frontier"]["development"]
    )
    thresholds = data["thresholds"]
    assert thresholds["scoring_run"] == scoring
    # every conversation has the same shape, hence the same level in the scoring run
    db = database(workspace["root"])
    level = db.execute("SELECT DISTINCT level FROM conversation_scores").fetchone()[0]
    expected = sum(1 for i in range(N) if truth(i) == level)
    measured = thresholds["current"]["measured"]
    assert measured["development"]["correct"] + measured["validation"]["correct"] == expected
    assert thresholds["improvement_holds_on_validation"] in (True, False)
    metrics_rater = next(
        r
        for r in report(workspace, capsys, "s")[1]["data"]["raters"]
        if r["rater"] == f"metrics:scoring_run_{scoring}"
    )
    assert (metrics_rater["compared"], metrics_rater["accuracy"]) == (40, expected / 40)
    assert config_path.read_bytes() == before  # nothing was applied
    # the split is stable: the same call returns the same figures
    again = cli(workspace, capsys, "calibrate", "suggest", "--set", "s", *lowered)[1]["data"]
    assert again["policies"] == data["policies"] and again["thresholds"] == thresholds
    with out.open(encoding="utf-8", newline="") as handle:
        kinds = {r["kind"] for r in csv.DictReader(handle)}
    assert kinds == {"policy", "threshold"}


def test_workflow_without_a_review_uses_the_primary_run_alone(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    db = database(workspace["root"])
    db.execute("DELETE FROM llm_reviews")
    db.close()
    sample(workspace, capsys, "s")
    import_all(workspace, capsys, "s")
    data = report(workspace, capsys, "s")[1]["data"]
    assert [r["rater"] for r in data["raters"]] == ["primary:fake/fake-classifier"]
    assert data["model_pair"] is None
    lowered = config(workspace, "[calibration]\nmin_labels_suggest = 30\n")
    suggested = cli(workspace, capsys, "calibrate", "suggest", "--set", "s", *lowered)[1]["data"]
    assert suggested["policies"] is None


def test_unknown_set_and_missing_workspace_errors(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = report(workspace, capsys, "nope")
    assert code == ExitCode.USAGE_ERROR and payload["error"]["code"] == "CALIBRATION_SET_NOT_FOUND"


# Configuration ------------------------------------------------------------------------------


def test_calibration_settings_are_validated() -> None:
    assert parse_settings({}).calibration.min_labels_report == 30
    assert parse_settings({}).calibration.min_labels_suggest == 50
    settings = parse_settings({"calibration": {"min_labels_report": 5, "validation_fraction": 0.4}})
    assert (settings.calibration.min_labels_report, settings.calibration.validation_fraction) == (
        5,
        0.4,
    )
    for bad in (
        {"nope": 1},
        {"min_labels_report": 0},
        {"validation_fraction": 0.9},
        {"min_labels_suggest": "x"},
    ):
        with pytest.raises(ConfigError):
            parse_settings({"calibration": bad})


# Human-readable output ----------------------------------------------------------------------


@pytest.mark.parametrize(("language", "phrase"), [("en", "Caveat"), ("pt-BR", "Ressalva")])
def test_every_command_prints_readable_output_in_both_languages(
    workspace: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    language: str,
    phrase: str,
) -> None:
    root, tmp = str(workspace["root"]), workspace["tmp"]
    run = str(workspace["primary"])

    def human(*args: str) -> str:
        assert main(["--lang", language, *args, "-w", root]) == 0
        captured = capsys.readouterr()
        return captured.out + captured.err

    assert "{" not in human(
        "calibrate", "sample", "--set", "h", "--size", "40", "--out", str(tmp / "h.csv")
    )
    filled = label_sheet(workspace, "h")
    assert "40" in human("calibrate", "import", "--set", "h", "--labels", str(filled))
    shown = human("calibrate", "show", "--set", "h", "--version", str(workspace["versions"][0]))
    assert "reply 0" in shown
    out = human("calibrate", "report", "--set", "h", "--out", str(tmp / "hr.csv"))
    assert "0.8" in out and phrase in out and "{" not in out
    assert "{" not in human("calibrate", "disagreements", "--out", str(tmp / "hd.csv"))
    out = human(
        "calibrate", "compare", "--set", "h", "--run", run, "--run", str(workspace["reviewer"])
    )
    assert "{" not in out and "0.75" in out
    lowered = config(workspace, "[calibration]\nmin_labels_suggest = 30\n")
    out = human("calibrate", "suggest", "--set", "h", *lowered)
    assert "{" not in out


# Regressions from the external audit ---------------------------------------------------------


def test_sets_items_and_labels_resist_update_delete_replace_and_late_inserts(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    sample(workspace, capsys, "s", 10)
    import_all(workspace, capsys, "s")
    db = database(workspace["root"])
    snapshot = {
        table: db.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
        for table in ("calibration_sets", "calibration_items", "calibration_labels")
    }
    set_id, version = db.execute("SELECT set_id, version_id FROM calibration_items").fetchone()
    unsampled = next(
        v
        for v in workspace["versions"].values()
        if v not in {r[1] for r in snapshot["calibration_items"]}
    )
    attacks = [
        "UPDATE calibration_sets SET seed = 99",
        "UPDATE calibration_sets SET scoring_run_id = NULL",
        "DELETE FROM calibration_sets",
        "INSERT OR REPLACE INTO calibration_sets (id, name, sampling, seed, primary_run_id, "
        "population, size, created_at, tool_version) "
        f"VALUES ({set_id}, 'zz', 'prioritized', 1, 1, 1, 1, 'x', 'x')",
        "INSERT OR REPLACE INTO calibration_sets (name, sampling, seed, primary_run_id, "
        "population, size, created_at, tool_version) "
        "VALUES ('s', 'prioritized', 1, 1, 1, 1, 'x', 'x')",
        "UPDATE calibration_items SET stratum = 'x'",
        "DELETE FROM calibration_items",
        f"INSERT OR REPLACE INTO calibration_items VALUES ({set_id}, {version}, 'x')",
        f"INSERT OR IGNORE INTO calibration_items VALUES ({set_id}, {version}, 'x')",
        f"INSERT INTO calibration_items VALUES ({set_id}, {unsampled}, 'late')",  # set is complete
        "UPDATE calibration_labels SET label = 'LOW'",
        "DELETE FROM calibration_labels",
        "INSERT OR REPLACE INTO calibration_labels "
        f"VALUES ({set_id}, {version}, 'UNCLEAR', 'h', 'x')",
    ]
    for statement in attacks:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(statement)
    for table, rows in snapshot.items():
        assert db.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall() == rows, table


def test_a_set_keeps_its_scoring_run_when_a_newer_one_exists(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    first = link_scoring_run(workspace, capsys)
    sample(workspace, capsys, "s")
    import_all(workspace, capsys, "s")
    db = database(workspace["root"])
    assert db.execute("SELECT scoring_run_id FROM calibration_sets").fetchone()[0] == first
    old_level = db.execute("SELECT DISTINCT level FROM conversation_scores").fetchone()[0]
    db.close()
    thresholds = "high = 0.02\nmedium = 0.01" if old_level == "LOW" else "high = 1.0\nmedium = 0.99"
    scoring_config = config(workspace, f"[scoring.thresholds]\n{thresholds}\n")
    assert (
        run_cli(["metrics", "run", "-w", str(workspace["root"]), *scoring_config], capsys)[0] == 0
    )
    db = database(workspace["root"])
    latest, new_level = db.execute(
        "SELECT r.id, s.level FROM scoring_runs r JOIN conversation_scores s ON s.run_id = r.id "
        "ORDER BY r.last_used_seq DESC, r.id DESC LIMIT 1"
    ).fetchone()
    db.close()
    assert latest != first and new_level != old_level  # the latest run really differs
    expected = sum(1 for i in range(N) if truth(i) == old_level) / N
    data = report(workspace, capsys, "s")[1]["data"]
    metrics = next(r for r in data["raters"] if r["kind"] == "metrics")
    assert metrics["rater"] == f"metrics:scoring_run_{first}" and metrics["accuracy"] == expected
    out = workspace["tmp"] / "d.csv"
    summary = cli(workspace, capsys, "calibrate", "disagreements", "--set", "s", "--out", str(out))[
        1
    ]["data"]
    assert summary["metrics_scoring_run"] == first
    # with the review link removed, the set still uses its own run, not the latest
    db = database(workspace["root"])
    db.execute("DELETE FROM llm_reviews")
    db.close()
    data = report(workspace, capsys, "s")[1]["data"]
    metrics = next(r for r in data["raters"] if r["kind"] == "metrics")
    assert metrics["rater"] == f"metrics:scoring_run_{first}" and metrics["accuracy"] == expected
    # a set drawn now (no link, no recorded run) fixes the latest run explicitly and says so
    sample(workspace, capsys, "later")
    db = database(workspace["root"])
    assert (
        db.execute("SELECT scoring_run_id FROM calibration_sets WHERE name = 'later'").fetchone()[0]
        == latest
    )


def test_the_sheet_of_an_existing_set_can_be_written_again_after_a_failed_write(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = "whatsapp_analyzer.cli.commands.calibrate.atomic_write_files"

    def full_disk(*outputs: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(target, full_disk)
    code, payload = sample(workspace, capsys, "a", 12, "--seed", "3")
    assert code == ExitCode.OUTPUT_ERROR and payload["error"]["code"] == "OUTPUT_NOT_WRITABLE"
    assert payload["error"]["details"] == {"set": "a", "set_created": True}
    assert "calibrate sheet --set a" in payload["error"]["hint"]
    assert not (workspace["tmp"] / "a.csv").exists()
    assert (
        database(workspace["root"]).execute("SELECT count(*) FROM calibration_sets").fetchone()[0]
        == 1
    )
    monkeypatch.setattr(target, atomic_write_files)
    assert sample(workspace, capsys, "a", 12)[1]["error"]["code"] == "SET_EXISTS"  # not redrawn
    out = workspace["tmp"] / "a.csv"
    code, payload = cli(workspace, capsys, "calibrate", "sheet", "--set", "a", "--out", str(out))
    assert code == 0, payload
    again = cli(workspace, capsys, "calibrate", "sheet", "--set", "a", "--out", str(out))
    assert again[1]["error"]["code"] == "OUTPUT_EXISTS"
    assert sample(workspace, capsys, "same", 12, "--seed", "3")[0] == 0
    assert [r["version_id"] for r in read_sheet(workspace, "a")] == [
        r["version_id"] for r in read_sheet(workspace, "same")
    ]
    assert (
        cli(
            workspace,
            capsys,
            "calibrate",
            "sheet",
            "--set",
            "nope",
            "--out",
            str(workspace["tmp"] / "n.csv"),
        )[1]["error"]["code"]
        == "CALIBRATION_SET_NOT_FOUND"
    )
    import_all(workspace, capsys, "a")  # the re-exported sheet is importable
    assert (
        report(
            workspace, capsys, "a", *config(workspace, "[calibration]\nmin_labels_report = 10\n")
        )[0]
        == 0
    )


def test_suggest_needs_enough_comparable_predictions_in_the_held_out_part(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert not improvement_holds(1.0, 0.5, 3, 20)  # better, but only 3 comparable predictions
    assert improvement_holds(1.0, 0.5, 10, 20)
    assert not improvement_holds(1.0, 0.5, 10, 5)  # held-out part too small
    assert not improvement_holds(0.5, 0.5, 10, 20) and not improvement_holds(None, 0.5, 10, 20)
    link_scoring_run(workspace, capsys)
    sample(workspace, capsys, "s")
    import_all(workspace, capsys, "s")
    keep = tuple(list(workspace["versions"].values())[:5])
    db = database(workspace["root"])  # the metric abstains almost everywhere
    db.execute(
        "UPDATE conversation_scores SET level = 'INSUFFICIENT_DATA', score = NULL "
        f"WHERE version_id NOT IN {keep}"
    )
    db.close()
    cfg = config(workspace, "[calibration]\nmin_labels_suggest = 30\nvalidation_fraction = 0.5\n")
    data = cli(workspace, capsys, "calibrate", "suggest", "--set", "s", *cfg)[1]["data"]
    assert data["validation_items"] >= 10  # enough items, yet not enough comparable predictions
    assert (
        "validation_too_small" not in data["caveats"]
        and "validation_few_comparable" in data["caveats"]
    )
    thresholds = data["thresholds"]
    assert max(thresholds["validation_comparable"].values()) <= 5
    assert thresholds["improvement_holds_on_validation"] is False


def test_compare_does_not_pair_items_without_a_recorded_context_hash(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    sample(workspace, capsys, "s")
    import_all(workspace, capsys, "s")
    v = workspace["versions"]
    db = database(workspace["root"])
    second = insert_run(db, "second-model", {v[i]: truth(i) for i in range(N)})
    db.execute(  # NULL in both runs: equal NULLs must not count as the same context
        "UPDATE llm_jobs SET context_hash = NULL WHERE version_id IN (?, ?)", (v[10], v[11])
    )
    db.execute(  # NULL in one run only
        "UPDATE llm_jobs SET context_hash = NULL WHERE run_id = ? AND version_id = ?",
        (second, v[12]),
    )
    db.close()
    code, payload = cli(
        workspace,
        capsys,
        "calibrate",
        "compare",
        "--set",
        "s",
        "--run",
        str(workspace["primary"]),
        "--run",
        str(second),
    )
    assert code == 0, payload
    data = payload["data"]
    assert data["excluded_by_reason"] == {"context_unverified": 3}
    assert data["paired"] == 37 and "context_unverified" in data["caveats"]
