"""Read-only views of a workspace: status, run comparison and CSV export.

Each view reads inside one transaction (`read_snapshot`), so it reflects a single committed
state even while `metrics run` keeps committing batches. Status and comparison return aggregate
counts only: no conversation keys, senders or message text.
"""

import csv
import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from whatsapp_analyzer import __version__
from whatsapp_analyzer.fileio import atomic_write_files
from whatsapp_analyzer.hashing import short
from whatsapp_analyzer.ingest.csv_export import escape_formula
from whatsapp_analyzer.metrics.runner import (
    RAW_COLUMNS,
    current_versions,
    input_fingerprint,
    level_counts,
)
from whatsapp_analyzer.metrics.scoring import Level
from whatsapp_analyzer.store.db import read_snapshot, schema_version, utc_now

RECENT_RUNS = 20
_CURRENT_IDS = "SELECT current_version_id FROM conversations WHERE current_version_id IS NOT NULL"
EXPORT_SCHEMA = 1
SCORE_COLUMNS = ("frequency", "reciprocity", "initiative", "recency", "coverage")
EXPORT_COLUMNS = (
    "conversation_key",
    "source_format",
    "content_sha256",
    "data_version",
    "run_id",
    "run_status",
    "run_current_data",
    "reference_date",
    "metrics_hash",
    "scoring_hash",
    *(c for c in RAW_COLUMNS),
    *SCORE_COLUMNS,
    "score",
    "level",
    "reason",
)


class RunNotFoundError(Exception):
    def __init__(self, run_id: int | None) -> None:
        super().__init__(str(run_id))
        self.run_id = run_id


@dataclass
class ExportResult:
    path: Path
    metadata_path: Path
    rows: int
    metadata: dict[str, object]


# Status -----------------------------------------------------------------------------------


def workspace_status(connection: sqlite3.Connection) -> dict[str, object]:
    with read_snapshot(connection):
        fingerprint = input_fingerprint(current_versions(connection))
        one = _scalar_query(connection)
        formats = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT source_format, count(*) FROM conversations GROUP BY source_format"
            )
        }
        runs = [
            _run_summary(connection, row, fingerprint)
            for row in connection.execute(
                "SELECT * FROM scoring_runs ORDER BY id DESC LIMIT ?", (RECENT_RUNS,)
            ).fetchall()
        ]
        return {
            "schema_version": schema_version(connection),
            "conversations": {
                "total": one("SELECT count(*) FROM conversations"),
                "groups": one(
                    "SELECT count(*) FROM conversations c JOIN conversation_versions v "
                    "ON v.id = c.current_version_id WHERE v.is_group = 1"
                ),
                "with_earlier_versions": one(
                    "SELECT count(DISTINCT conversation_id) FROM conversation_versions v "
                    f"WHERE v.id NOT IN ({_CURRENT_IDS})"
                ),
                "by_format": formats,
                "keys_in_several_formats": one(
                    "SELECT count(*) FROM (SELECT conversation_key FROM conversations "
                    "GROUP BY conversation_key HAVING count(*) > 1)"
                ),
            },
            "messages_current": one(
                "SELECT count(*) FROM conversations c "
                "JOIN messages m ON m.version_id = c.current_version_id"
            ),
            "versions_superseded": one(
                f"SELECT count(*) FROM conversation_versions WHERE id NOT IN ({_CURRENT_IDS})"
            ),
            "imports": one("SELECT count(*) FROM imports"),
            "last_import_at": one("SELECT max(finished_at) FROM imports"),
            "metric_configs": one("SELECT count(*) FROM metric_configs"),
            "runs_total": one("SELECT count(*) FROM scoring_runs"),
            "runs": runs,
        }


def _scalar_query(connection: sqlite3.Connection) -> Callable[[str], object]:
    def query(sql: str) -> object:
        return connection.execute(sql).fetchone()[0]

    return query


def _run_summary(
    connection: sqlite3.Connection, run: sqlite3.Row, fingerprint: str
) -> dict[str, object]:
    return {
        "id": run["id"],
        "status": run["status"],
        "partial": _is_partial(run),
        "current_data": run["input_fingerprint"] == fingerprint,
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "reference_date": run["reference_date"],
        "reference_date_source": run["reference_date_source"],
        "metrics_hash": short(run["metrics_hash"]),
        "scoring_hash": short(run["scoring_hash"]),
        "conversations_scored": run["conversations_scored"],
        "conversations_total": run["conversations_total"],
        "levels": level_counts(connection, run["id"]),
    }


def _is_partial(run: sqlite3.Row) -> bool:
    return bool(
        run["status"] != "complete" or run["conversations_scored"] < run["conversations_total"]
    )


def _load_run(
    connection: sqlite3.Connection, run_id: int | None, fingerprint: str | None = None
) -> sqlite3.Row:
    """A run by id, or by default the run created or reused most recently, preferring complete
    runs that match the current data."""
    if run_id is None:
        row = connection.execute(
            "SELECT * FROM scoring_runs ORDER BY "
            "(status = 'complete' AND input_fingerprint = ?) DESC, last_used_seq DESC, id DESC "
            "LIMIT 1",
            (fingerprint or "",),
        ).fetchone()
    else:
        row = connection.execute("SELECT * FROM scoring_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise RunNotFoundError(run_id)
    return row  # type: ignore[no-any-return]


# Comparison --------------------------------------------------------------------------------


def compare_runs(connection: sqlite3.Connection, run_a: int, run_b: int) -> dict[str, object]:
    """Aggregate agreement between two runs over the conversations both scored on the same
    data version. Conversations whose data changed between the runs are counted, not
    compared, because their differences would mix data changes with configuration changes."""
    with read_snapshot(connection):
        first, second = _load_run(connection, run_a), _load_run(connection, run_b)
        scores_a, scores_b = (
            _scores_by_conversation(connection, run_a),
            _scores_by_conversation(connection, run_b),
        )
        metrics_a = _metrics_config(connection, first["metrics_hash"])
        metrics_b = _metrics_config(connection, second["metrics_hash"])

    shared = scores_a.keys() & scores_b.keys()
    same_data = [c for c in shared if scores_a[c][0] == scores_b[c][0]]
    transitions: Counter[tuple[str, str]] = Counter(
        (scores_a[c][1], scores_b[c][1]) for c in same_data
    )
    differences = [
        abs(a - b)
        for c in same_data
        if (a := scores_a[c][2]) is not None and (b := scores_b[c][2]) is not None
    ]
    agreeing = sum(count for (a, b), count in transitions.items() if a == b)
    levels = [level.value for level in Level]
    return {
        "run_a": _compare_side(first, scores_a),
        "run_b": _compare_side(second, scores_b),
        "changed_settings": sorted(
            _changed_keys(
                json.loads(first["scoring_json"]), json.loads(second["scoring_json"]), "scoring."
            )
            | _changed_keys(metrics_a, metrics_b, "metrics.")
        ),
        "compared": len(same_data),
        "only_in_a": len(scores_a.keys() - scores_b.keys()),
        "only_in_b": len(scores_b.keys() - scores_a.keys()),
        "different_data_version": len(shared) - len(same_data),
        "agreement": round(agreeing / len(same_data), 4) if same_data else None,
        "transitions": {a: {b: transitions[(a, b)] for b in levels} for a in levels},
        "mean_absolute_score_difference": (
            round(sum(differences) / len(differences), 4) if differences else None
        ),
    }


def _scores_by_conversation(
    connection: sqlite3.Connection, run_id: int
) -> dict[int, tuple[int, str, float | None]]:
    return {
        row[0]: (row[1], row[2], row[3])
        for row in connection.execute(
            "SELECT v.conversation_id, s.version_id, s.level, s.score FROM conversation_scores s "
            "JOIN conversation_versions v ON v.id = s.version_id WHERE s.run_id = ?",
            (run_id,),
        )
    }


def _compare_side(
    run: sqlite3.Row, scores: dict[int, tuple[int, str, float | None]]
) -> dict[str, object]:
    counts = Counter(level for _, level, _ in scores.values())
    return {
        "id": run["id"],
        "status": run["status"],
        "partial": _is_partial(run),
        "reference_date": run["reference_date"],
        "levels": {level.value: counts[level.value] for level in Level},
    }


def _metrics_config(connection: sqlite3.Connection, metrics_hash: str) -> dict[str, object]:
    row = connection.execute(
        "SELECT config_json FROM metric_configs WHERE hash = ?", (metrics_hash,)
    ).fetchone()
    config: dict[str, object] = json.loads(row[0]) if row else {}
    return config


def _changed_keys(a: object, b: object, prefix: str) -> set[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        changed: set[str] = set()
        for key in a.keys() | b.keys():
            changed |= _changed_keys(a.get(key), b.get(key), f"{prefix}{key}.")
        return changed
    return set() if a == b else {prefix.rstrip(".")}


# Export -----------------------------------------------------------------------------------


def export_run(
    connection: sqlite3.Connection,
    run_id: int | None,
    path: Path,
    on_snapshot: Callable[[], None] | None = None,
) -> ExportResult:
    """Write one CSV row per scored conversation of a run, plus `<file>.meta.json`.

    Rows and metadata come from the same snapshot. A run that is still running (or was
    interrupted) is exported as it was at that moment and marked `partial`. Rows computed from
    an earlier version of a conversation are marked `data_version = superseded`.
    """
    with read_snapshot(connection):
        fingerprint = input_fingerprint(current_versions(connection))
        run = _load_run(connection, run_id, fingerprint)
        if on_snapshot:
            on_snapshot()  # test seam: lets a test commit concurrent writes here
        rows = connection.execute(
            "SELECT c.conversation_key, c.source_format, v.content_sha256, "
            "(c.current_version_id = v.id) AS is_current, m.*, s.* "
            "FROM conversation_scores s "
            "JOIN conversation_versions v ON v.id = s.version_id "
            "JOIN conversations c ON c.id = v.conversation_id "
            "JOIN conversation_metrics m ON m.version_id = s.version_id AND m.metrics_hash = ? "
            "WHERE s.run_id = ? ORDER BY c.source_format, c.conversation_key",
            (run["metrics_hash"], run["id"]),
        ).fetchall()
        metrics_config = _metrics_config(connection, run["metrics_hash"])

    superseded = sum(1 for row in rows if not row["is_current"])
    metadata: dict[str, object] = {
        "schema": EXPORT_SCHEMA,
        "exported_at": utc_now(),
        "tool_version": __version__,
        "run": {
            "id": run["id"],
            "status": run["status"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "reference_date": run["reference_date"],
            "reference_date_source": run["reference_date_source"],
            "metrics_hash": run["metrics_hash"],
            "scoring_hash": run["scoring_hash"],
            "input_fingerprint": run["input_fingerprint"],
            "metrics_config": metrics_config,
            "scoring_config": json.loads(run["scoring_json"]),
        },
        "partial": _is_partial(run),
        "conversations_total": run["conversations_total"],
        "rows": len(rows),
        "current_data": run["input_fingerprint"] == fingerprint,
        "superseded_rows": superseded,
    }
    metadata_path = path.with_name(path.name + ".meta.json")
    run_state = (run["status"], "true" if metadata["current_data"] else "false")
    atomic_write_files(
        (path, _csv_lines(_export_row(row, run, run_state) for row in rows)),
        (metadata_path, [json.dumps(metadata, indent=2, sort_keys=True) + "\n"]),
    )
    return ExportResult(path, metadata_path, len(rows), metadata)


def _export_row(row: sqlite3.Row, run: sqlite3.Row, run_state: tuple[str, str]) -> list[str]:
    values: list[str] = [
        escape_formula(row["conversation_key"]),
        row["source_format"],
        row["content_sha256"],
        "current" if row["is_current"] else "superseded",
        str(run["id"]),
        *run_state,
        run["reference_date"],
        run["metrics_hash"],
        run["scoring_hash"],
    ]
    values += [
        _timestamp_cell(row[name]) if name == "last_inbound_at" else _cell(row[name])
        for name in RAW_COLUMNS
    ]
    values += [_cell(row[name], 6) for name in SCORE_COLUMNS]
    values += [_cell(row["score"], 4), row["level"], row["reason"]]
    return values


def _timestamp_cell(value: str | None) -> str:
    return datetime.fromisoformat(value).isoformat() if value else ""


def _cell(value: object, digits: int | None = None) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.{digits or 6}f}"
    return str(value)


def _csv_lines(rows: Iterable[list[str]]) -> Iterable[str]:
    class _Line:
        def __init__(self) -> None:
            self.value = ""

        def write(self, text: str) -> int:
            self.value = text
            return len(text)

    line = _Line()
    writer = csv.writer(line, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    yield line.value
    for row in rows:
        writer.writerow(row)
        yield line.value
