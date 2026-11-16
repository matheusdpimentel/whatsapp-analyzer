"""Calibration sets: drawing the sample, the review sheet, importing labels, showing a version.

A set fixes which conversation versions people label and how they were drawn:

- `representative`: a seeded simple random sample of the primary run's labeled versions, so
  agreement figures may be read as an estimate for that population (with their uncertainty);
- `prioritized`: versions where models or metrics diverge first, then random ones. Useful to
  find errors and train reviewers; its figures are biased and never a general performance.

Items and labels are immutable. The sheet and every report carry ids and labels only; message
text is shown on the terminal by `show` and is never written to a file by this tool.
"""

import csv
import hashlib
import io
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from whatsapp_analyzer import __version__
from whatsapp_analyzer.calibration.common import (
    HUMAN_LABELS,
    CalibrationError,
    Item,
    divergence_kinds,
    evidence,
    set_items,
    usable,
)
from whatsapp_analyzer.ingest.csv_export import escape_formula, unescape_formula
from whatsapp_analyzer.runner import review as reviews
from whatsapp_analyzer.store.db import transaction, utc_now

SHEET_COLUMNS = ("set", "conversation_key", "version_id", "data_version", "human_label")
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
STRATEGIES = ("representative", "prioritized")
_UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
MAX_ERRORS = 10


def draw_key(seed: int, version_id: int) -> str:
    """A stable pseudo-random order: the same seed and version always sort the same way."""
    return hashlib.sha256(f"{seed}:{version_id}".encode()).hexdigest()


def create_set(
    connection: sqlite3.Connection,
    name: str,
    strategy: str,
    size: int,
    seed: int,
    primary: sqlite3.Row,
    review_run_id: int | None,
) -> sqlite3.Row:
    if not NAME_PATTERN.fullmatch(name):
        raise CalibrationError("INVALID_SET_NAME", set=name)
    if strategy not in STRATEGIES:
        raise CalibrationError("INVALID_STRATEGY", value=strategy, choices=", ".join(STRATEGIES))
    if size < 1:
        raise CalibrationError("INVALID_SIZE", size=size)
    if connection.execute("SELECT 1 FROM calibration_sets WHERE name = ?", (name,)).fetchone():
        raise CalibrationError("SET_EXISTS", set=name)
    link: sqlite3.Row | None
    try:
        link = reviews.latest_review(connection, primary["id"], review_run_id)
    except reviews.ReviewError:
        if review_run_id is not None:
            raise CalibrationError("REVIEW_NOT_FOUND", review_run=review_run_id) from None
        link = None
    scoring_run = recorded_scoring_run(connection, link)
    facts = evidence(connection, primary["id"], link, scoring_run)
    population = {v: e for v, e in facts.items() if e.primary is not None}
    if size > len(population):
        raise CalibrationError("POPULATION_TOO_SMALL", size=size, population=len(population))
    ranked = sorted(population, key=lambda v: draw_key(seed, v))
    if strategy == "prioritized":
        flagged = [v for v in ranked if divergence_kinds(population[v])]
        rest = [v for v in ranked if v not in set(flagged)]
        chosen = [(v, "divergence") for v in flagged] + [(v, "other") for v in rest]
    else:
        chosen = [(v, "random") for v in ranked]
    chosen = chosen[:size]
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO calibration_sets (name, sampling, seed, primary_run_id, review_run_id, "
            "scoring_run_id, population, size, created_at, tool_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                strategy,
                seed,
                primary["id"],
                None if link is None else link["review_run_id"],
                scoring_run,
                len(population),
                len(chosen),
                utc_now(),
                __version__,
            ),
        )
        set_id = cursor.lastrowid
        connection.executemany(
            "INSERT INTO calibration_items (set_id, version_id, stratum) VALUES (?, ?, ?)",
            [(set_id, version, stratum) for version, stratum in chosen],
        )
    row: sqlite3.Row = connection.execute(
        "SELECT * FROM calibration_sets WHERE id = ?", (set_id,)
    ).fetchone()
    return row


def recorded_scoring_run(connection: sqlite3.Connection, link: sqlite3.Row | None) -> int | None:
    """The scoring run a new set is tied to for good: the one recorded with the review, else
    the latest complete one *at this moment*. Later reports use the stored id, never the latest."""
    if link is not None:
        recorded = json.loads(link["criteria_json"]).get("scoring_run")
        if recorded is not None:
            return int(recorded)
    return reviews.reference_scoring_run(connection)


def sheet_lines(connection: sqlite3.Connection, set_row: sqlite3.Row) -> Iterator[str]:
    """The review sheet: ids only. It never shows model labels (reviewers stay blind to them)
    and never message text."""
    yield _csv_line(list(SHEET_COLUMNS))
    for item in set_items(connection, set_row["id"]):
        yield _csv_line(
            [
                set_row["name"],
                escape_formula(item.key),
                str(item.version_id),
                "current" if item.current else "superseded",
                item.human or "",
            ]
        )


def sheet_metadata(set_row: sqlite3.Row) -> dict[str, object]:
    return {
        "schema": 1,
        "set": set_row["name"],
        "sampling": set_row["sampling"],
        "generalizable": set_row["sampling"] == "representative",
        "seed": set_row["seed"],
        "population": set_row["population"],
        "size": set_row["size"],
        "primary_run": set_row["primary_run_id"],
        "review_run": set_row["review_run_id"],
        "scoring_run": set_row["scoring_run_id"],
        "tool_version": __version__,
        "content_included": False,
        "labels": list(HUMAN_LABELS),
        "note": "Fill human_label with HIGH, MEDIUM, LOW or UNCLEAR (exactly). Read each "
        "conversation locally with `calibrate show`. Leave blank what is not labeled yet.",
    }


def import_labels(
    connection: sqlite3.Connection, set_row: sqlite3.Row, path: Path
) -> dict[str, int]:
    """All-or-nothing import. Blank labels are pending; labels outside the vocabulary,
    unknown or edited ids and duplicate rows are errors; a label that differs from one already
    stored is refused (labels are immutable)."""
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise CalibrationError("LABELS_UNREADABLE", path=str(path)) from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    missing = {"conversation_key", "version_id", "human_label"} - set(reader.fieldnames or ())
    if missing:
        raise CalibrationError("LABELS_COLUMNS", missing=", ".join(sorted(missing)))
    items = {item.version_id: item for item in set_items(connection, set_row["id"])}
    parsed, pending, problems = _parse_rows(reader, items, set_row["name"])
    stored = {i.version_id: i.human for i in items.values() if i.human is not None}
    for version, label in parsed.items():
        if version in stored and stored[version] != label:
            problems.append({"line": 0, "reason": "label_conflict"})
    if problems:
        raise CalibrationError(
            "LABELS_INVALID", count=len(problems), problems=problems[:MAX_ERRORS]
        )
    fresh = {v: label for v, label in parsed.items() if v not in stored}
    digest = hashlib.sha256(raw).hexdigest()
    with transaction(connection):
        connection.executemany(
            "INSERT INTO calibration_labels (set_id, version_id, label, labels_sha256, "
            "imported_at) VALUES (?, ?, ?, ?, ?)",
            [(set_row["id"], v, label, digest, utc_now()) for v, label in fresh.items()],
        )
    total = set_items(connection, set_row["id"])
    return {
        "new": len(fresh),
        "unchanged": len(parsed) - len(fresh),
        "pending_in_file": pending,
        "labeled": sum(1 for i in total if i.human is not None),
        "usable": len(usable(total)),
        "unclear": sum(1 for i in total if i.human == "UNCLEAR"),
        "items": len(total),
    }


def _parse_rows(
    reader: "csv.DictReader[str]", items: dict[int, Item], set_name: str
) -> tuple[dict[int, str], int, list[dict[str, object]]]:
    problems: list[dict[str, object]] = []
    seen: set[int] = set()
    parsed: dict[int, str] = {}
    pending = 0
    for line, record in enumerate(reader, start=2):
        reason = None
        version = None
        if record.get("set") not in (None, "", set_name):
            reason = "other_set"
        else:
            try:
                version = int(record["version_id"])
            except (TypeError, ValueError):
                reason = "invalid_version_id"
        item = None if version is None else items.get(version)
        label = (record.get("human_label") or "").strip()
        if reason is None and item is None:
            reason = "unknown_version_id"
        elif reason is None and item is not None:
            if unescape_formula(record["conversation_key"] or "") != item.key:
                reason = "key_mismatch"
            elif version in seen:
                reason = "duplicate_row"
            elif label and label not in HUMAN_LABELS:
                reason = "invalid_label"
        if reason is not None:
            problems.append({"line": line, "reason": reason})
            continue
        assert version is not None
        seen.add(version)
        if label:
            parsed[version] = label
        else:
            pending += 1
    return parsed, pending, problems


def show(
    connection: sqlite3.Connection,
    set_row: sqlite3.Row,
    version_id: int | None,
    key: str | None,
    offset: int,
    limit: int,
    include_text: bool,
) -> dict[str, object]:
    """The messages of one version of the set, for the person who labels it. Model labels and
    metrics are deliberately not shown. Control and bidi characters are replaced so a
    message cannot rewrite the terminal. Without `include_text` (the `--json` case) senders and
    texts are left out: only ids, times and types are returned."""
    if (version_id is None) == (key is None):
        raise CalibrationError("SHOW_TARGET")
    items = set_items(connection, set_row["id"])
    matches = [i for i in items if i.version_id == version_id or (key and i.key == key)]
    if not matches:
        raise CalibrationError("ITEM_NOT_IN_SET", set=set_row["name"])
    if len(matches) > 1:
        raise CalibrationError("AMBIGUOUS_KEY", versions=[i.version_id for i in matches])
    item = matches[0]
    total = connection.execute(
        "SELECT count(*) FROM messages WHERE version_id = ?", (item.version_id,)
    ).fetchone()[0]
    messages = [
        {
            "seq": r["seq"],
            "timestamp": r["timestamp"],
            "direction": r["direction"] or "",
            "type": r["message_type"],
            **({"sender": _clean(r["sender"]), "text": _clean(r["text"])} if include_text else {}),
        }
        for r in connection.execute(
            "SELECT seq, timestamp, sender, direction, message_type, text FROM messages "
            "WHERE version_id = ? ORDER BY seq LIMIT ? OFFSET ?",
            (item.version_id, limit, offset),
        )
    ]
    return {
        "set": set_row["name"],
        "version_id": item.version_id,
        "conversation_key": item.key,
        "data_version": "current" if item.current else "superseded",
        "total_messages": total,
        "offset": offset,
        "content_included": include_text,
        "messages": messages,
    }


def _clean(text: str) -> str:
    return _UNSAFE.sub("�", text)


def _csv_line(cells: list[str]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerow(cells)
    return buffer.getvalue()
