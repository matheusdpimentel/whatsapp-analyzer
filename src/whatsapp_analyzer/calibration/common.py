"""Shared reads for calibration: sets, human labels, run results and model/metric evidence.

Everything here only reads existing tables; runs, reviews and scores are never modified.
"""

import csv
import io
import json
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from whatsapp_analyzer import __version__
from whatsapp_analyzer.calibration.stats import ORDER
from whatsapp_analyzer.config import ConsolidationPolicy
from whatsapp_analyzer.fileio import atomic_write_files
from whatsapp_analyzer.runner import review as reviews
from whatsapp_analyzer.store.db import utc_now

HUMAN_LABELS = ("HIGH", "MEDIUM", "LOW", "UNCLEAR")
POLICIES = tuple(ConsolidationPolicy)


class CalibrationError(Exception):
    """An expected failure with a stable `code`; `details` never holds message text."""

    def __init__(self, code: str, **details: object) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


def get_set(connection: sqlite3.Connection, name: str) -> sqlite3.Row:
    row: sqlite3.Row | None = connection.execute(
        "SELECT * FROM calibration_sets WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        raise CalibrationError("CALIBRATION_SET_NOT_FOUND", set=name)
    return row


@dataclass(frozen=True)
class Item:
    version_id: int
    key: str
    current: bool
    stratum: str
    human: str | None  # HIGH, MEDIUM, LOW, UNCLEAR, or None while unlabeled


def set_items(connection: sqlite3.Connection, set_id: int) -> list[Item]:
    rows = connection.execute(
        "SELECT i.version_id, c.conversation_key, (c.current_version_id = i.version_id) AS cur, "
        "i.stratum, l.label FROM calibration_items i "
        "JOIN conversation_versions v ON v.id = i.version_id "
        "JOIN conversations c ON c.id = v.conversation_id "
        "LEFT JOIN calibration_labels l ON l.set_id = i.set_id AND l.version_id = i.version_id "
        "WHERE i.set_id = ? ORDER BY c.source_format, c.conversation_key, i.version_id",
        (set_id,),
    )
    return [Item(r[0], r[1], bool(r[2]), r[3], r[4]) for r in rows]


def usable(items: Sequence[Item]) -> list[Item]:
    """Items whose human label can be compared (HIGH, MEDIUM or LOW)."""
    return [item for item in items if item.human in ORDER]


def require_labels(items: Sequence[Item], minimum: int, command: str) -> None:
    have = len(usable(items))
    if have < minimum:
        raise CalibrationError("CALIBRATION_REQUIRED", command=command, have=have, need=minimum)


@dataclass(frozen=True)
class RunJob:
    status: str
    label: str | None  # a valid label (succeeded or reused), else None
    context_hash: str | None
    prompt_hash: str | None


def run_jobs(connection: sqlite3.Connection, run_id: int) -> dict[int, RunJob]:
    """version_id -> result of that run. A version the run never planned is absent."""
    result: dict[int, RunJob] = {}
    for r in connection.execute(
        "SELECT version_id, status, label, context_hash, prompt_hash FROM llm_jobs "
        "WHERE run_id = ?",
        (run_id,),
    ):
        valid = r["label"] if r["status"] in reviews.LABELED_STATUSES else None
        result[int(r["version_id"])] = RunJob(
            r["status"], valid, r["context_hash"], r["prompt_hash"]
        )
    return result


def get_run(connection: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    row: sqlite3.Row | None = connection.execute(
        "SELECT * FROM llm_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise CalibrationError("RUN_NOT_FOUND", run_id=run_id)
    return row


def run_description(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "status": row["status"],
        "provider": row["provider"],
        "model": row["model"],
        "prompt": f"{row['prompt_id']}@{row['prompt_version']}",
        "run_key": row["run_key"],
        "recorded_cost": reviews.run_cost(connection, row["id"]),
    }


def is_partial(row: sqlite3.Row) -> bool:
    return row["status"] in {"running", "paused", "interrupted", "budget_reached"}


def link_for(connection: sqlite3.Connection, set_row: sqlite3.Row) -> sqlite3.Row | None:
    """The recorded review link between the set's primary and review run, if any."""
    if set_row["review_run_id"] is None:
        return None
    try:
        return reviews.latest_review(
            connection, set_row["primary_run_id"], set_row["review_run_id"]
        )
    except reviews.ReviewError:
        return None


@dataclass(frozen=True)
class Evidence:
    """What the models and the metrics said about one version of the primary run."""

    version_id: int
    key: str
    current: bool
    primary: str | None
    reviewer: str | None  # only an independent reviewer result
    metric: str | None
    reviewed: bool


def evidence(
    connection: sqlite3.Connection,
    primary_id: int,
    link: sqlite3.Row | None,
    scoring_run: int | None,
) -> dict[int, Evidence]:
    """Per version of the primary run. With a review link, labels and independence come from
    `consolidate`; without it, from the primary run alone. Metric levels always come from the
    explicit `scoring_run` (a set's recorded one): the latest scoring run is never consulted."""
    keys = {
        r[0]: (r[1], bool(r[2]))
        for r in connection.execute(
            "SELECT j.version_id, c.conversation_key, (c.current_version_id = j.version_id) "
            "FROM llm_jobs j JOIN conversation_versions v ON v.id = j.version_id "
            "JOIN conversations c ON c.id = v.conversation_id WHERE j.run_id = ?",
            (primary_id,),
        )
    }
    result: dict[int, Evidence] = {}
    if link is not None:
        columns = reviews.COLUMNS
        for row in reviews.consolidate(connection, link, ConsolidationPolicy.HUMAN_REVIEW).rows:
            cells = dict(zip(columns, row, strict=True))
            version = int(cells["version_id"])
            independent = cells["reviewer_independent"] == "true"
            result[version] = Evidence(
                version,
                keys[version][0],
                keys[version][1],
                cells["primary_label"] or None,
                (cells["reviewer_label"] or None) if independent else None,
                None,
                cells["reviewed"] == "true",
            )
        return _with_levels(connection, result, scoring_run)
    jobs = run_jobs(connection, primary_id)
    for version, job in jobs.items():
        result[version] = Evidence(
            version, keys[version][0], keys[version][1], job.label, None, None, False
        )
    return _with_levels(connection, result, scoring_run)


def _with_levels(
    connection: sqlite3.Connection, found: dict[int, Evidence], scoring_run: int | None
) -> dict[int, Evidence]:
    levels = reviews.metric_levels(connection, list(found), scoring_run)
    return {v: replace(e, metric=levels.get(v)) for v, e in found.items()}


def consolidated(
    connection: sqlite3.Connection, link: sqlite3.Row, policy: ConsolidationPolicy
) -> dict[int, tuple[str | None, bool]]:
    """version_id -> (consolidated label or None, needs human review) under `policy`."""
    columns = reviews.COLUMNS
    out: dict[int, tuple[str | None, bool]] = {}
    for row in reviews.consolidate(connection, link, policy).rows:
        cells = dict(zip(columns, row, strict=True))
        out[int(cells["version_id"])] = (
            cells["consolidated_label"] or None,
            cells["needs_human_review"] == "true",
        )
    return out


def divergence_kinds(e: Evidence) -> list[str]:
    """Why a version deserves a human look. Pure; labels stay categories."""
    kinds: list[str] = []
    if e.primary is not None and e.reviewer is not None and e.primary != e.reviewer:
        kinds.append("model_disagreement")
    if e.reviewed and (e.primary is None) != (e.reviewer is None):
        kinds.append("one_result")
    if "REVIEW_REQUIRED" in (e.primary, e.reviewer):
        kinds.append("review_required")
    if e.primary in ORDER and e.metric in ORDER and e.primary != e.metric:
        kinds.append("metric_disagreement")
    return kinds


def sampling_caveats(set_row: sqlite3.Row, items: Sequence[Item], small_sample: int) -> list[str]:
    """Why a figure must not be read as general performance. Stable identifiers."""
    caveats: list[str] = []
    if set_row["sampling"] != "representative":
        caveats.append("prioritized_sample_not_generalizable")
    elif len(usable(items)) < small_sample:
        caveats.append("small_sample")
    if sum(1 for i in items if i.human is None):
        caveats.append("pending_labels")
    if any(not i.current for i in items):
        caveats.append("superseded_versions")
    if any(i.human == "UNCLEAR" for i in items):
        caveats.append("unclear_labels_excluded")
    return caveats


def write_export(
    path: Path, columns: Sequence[str], rows: Sequence[Sequence[str]], metadata: dict[str, object]
) -> Path:
    """A CSV and its `.meta.json`, written atomically. Neither holds message text."""
    meta_path = path.with_name(path.name + ".meta.json")
    meta = {
        "schema": 1,
        "exported_at": utc_now(),
        "tool_version": __version__,
        "content_included": False,
        **metadata,
    }

    def lines() -> Iterator[str]:
        for row in (columns, *rows):
            buffer = io.StringIO()
            csv.writer(buffer, lineterminator="\n").writerow(row)
            yield buffer.getvalue()

    atomic_write_files(
        (path, lines()), (meta_path, [json.dumps(meta, indent=2, sort_keys=True) + "\n"])
    )
    return meta_path
