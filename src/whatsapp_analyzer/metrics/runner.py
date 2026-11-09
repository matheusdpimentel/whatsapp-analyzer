"""Compute raw metrics and scores for the current version of every conversation.

A scoring run is identified by three hashes: the scoring configuration, the metrics
configuration (with its resolved reference date) and the input fingerprint (the current version
of every conversation). Running again with the same three hashes reuses a complete run or
resumes an incomplete one; any change creates a new run, and older runs stay queryable.

Work is committed in batches, so `export` and `status` can read finished results while a run is
still in progress. The caller must hold the workspace writer lock: a run found in `running`
state at start-up therefore belongs to a process that died, and is marked `incomplete`.
"""

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime
from enum import StrEnum
from itertools import groupby

from whatsapp_analyzer import __version__
from whatsapp_analyzer.config import (
    METRICS_VERSION,
    SCORING_VERSION,
    Settings,
    check_reference_date,
)
from whatsapp_analyzer.hashing import canonical_json, config_hash, format_timestamp, sha256_hex
from whatsapp_analyzer.metrics.raw import RawMetrics, StoredMessage, compute_raw_metrics
from whatsapp_analyzer.metrics.scoring import Level, score_conversation
from whatsapp_analyzer.schema import Direction, MediaKind, MessageType
from whatsapp_analyzer.store.db import transaction, utc_now

DEFAULT_BATCH_SIZE = 500
RAW_COLUMNS = tuple(f.name for f in fields(RawMetrics))

Progress = Callable[[int, int], None]  # conversations scored so far, total; after each commit


class ReferenceSource(StrEnum):
    OPTION = "option"
    CONFIG = "config"
    DATASET_MAX = "dataset_max"


class RunMode(StrEnum):
    NEW = "new"
    RESUMED = "resumed"
    REUSED = "reused"


class NothingToScoreError(Exception):
    pass


@dataclass(frozen=True)
class _Version:
    conversation_id: int
    version_id: int
    content_sha256: str
    is_group: bool


@dataclass
class RunResult:
    run_id: int
    mode: RunMode
    reference_date: date
    reference_source: ReferenceSource
    metrics_hash: str
    scoring_hash: str
    conversations_total: int
    metrics_computed: int = 0
    metrics_reused: int = 0
    levels: dict[str, int] | None = None


def run_metrics(
    connection: sqlite3.Connection,
    settings: Settings,
    reference_override: date | None = None,
    force: bool = False,
    progress: Progress | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> RunResult:
    with transaction(connection):
        connection.execute("UPDATE scoring_runs SET status = 'incomplete' WHERE status = 'running'")

    versions = current_versions(connection)
    if not versions:
        raise NothingToScoreError
    reference, source = _reference_date(connection, settings, reference_override)
    metrics_config = settings.metrics.resolved(reference)
    metrics_hash = config_hash("metrics", METRICS_VERSION, metrics_config)
    scoring_config = settings.scoring.to_dict()
    scoring_hash = config_hash("scoring", SCORING_VERSION, scoring_config)
    fingerprint = input_fingerprint(versions)

    result = RunResult(
        run_id=0,
        mode=RunMode.NEW,
        reference_date=reference,
        reference_source=source,
        metrics_hash=metrics_hash,
        scoring_hash=scoring_hash,
        conversations_total=len(versions),
    )
    with transaction(connection):
        connection.execute(
            "INSERT OR IGNORE INTO metric_configs (hash, metrics_version, config_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (metrics_hash, METRICS_VERSION, canonical_json(metrics_config), utc_now()),
        )
        previous = connection.execute(
            "SELECT id, status FROM scoring_runs WHERE scoring_hash = ? AND metrics_hash = ? "
            "AND input_fingerprint = ? ORDER BY id DESC LIMIT 1",
            (scoring_hash, metrics_hash, fingerprint),
        ).fetchone()
        if previous is not None and not force:
            result.run_id = previous["id"]
            if previous["status"] == "complete":
                result.mode = RunMode.REUSED
                _mark_used(connection, result.run_id)
            else:
                result.mode = RunMode.RESUMED
                connection.execute(
                    "UPDATE scoring_runs SET status = 'running', finished_at = NULL WHERE id = ?",
                    (result.run_id,),
                )
                _mark_used(connection, result.run_id)
        else:
            result.run_id = _create_run(
                connection,
                (scoring_hash, scoring_config, metrics_hash, reference, source, fingerprint),
                len(versions),
            )
            _mark_used(connection, result.run_id)

    if result.mode is not RunMode.REUSED:
        try:
            _score_pending(connection, versions, settings, result, (progress, batch_size))
        except BaseException:
            _set_status(connection, result.run_id, "incomplete")
            raise
        _set_status(connection, result.run_id, "complete")
    result.levels = level_counts(connection, result.run_id)
    return result


def input_fingerprint(versions: Sequence[_Version]) -> str:
    return sha256_hex(
        canonical_json(sorted([v.conversation_id, v.content_sha256] for v in versions))
    )


def level_counts(connection: sqlite3.Connection, run_id: int) -> dict[str, int]:
    counts = dict.fromkeys((level.value for level in Level), 0)
    for row in connection.execute(
        "SELECT level, count(*) AS n FROM conversation_scores WHERE run_id = ? GROUP BY level",
        (run_id,),
    ):
        counts[row["level"]] = row["n"]
    return counts


def current_versions(connection: sqlite3.Connection) -> list[_Version]:
    return [
        _Version(row[0], row[1], row[2], bool(row[3]))
        for row in connection.execute(
            "SELECT c.id, v.id, v.content_sha256, v.is_group FROM conversations c "
            "JOIN conversation_versions v ON v.id = c.current_version_id ORDER BY c.id"
        )
    ]


def _reference_date(
    connection: sqlite3.Connection, settings: Settings, override: date | None
) -> tuple[date, ReferenceSource]:
    if override is not None:
        return override, ReferenceSource.OPTION
    if settings.metrics.reference_date is not None:
        return settings.metrics.reference_date, ReferenceSource.CONFIG
    latest = connection.execute(
        "SELECT max(m.timestamp) FROM conversations c "
        "JOIN messages m ON m.version_id = c.current_version_id "
        "WHERE m.message_type != 'system'"
    ).fetchone()[0]
    if latest is None:
        raise NothingToScoreError
    resolved = datetime.fromisoformat(latest).date()
    return check_reference_date(resolved, "metrics.reference_date"), ReferenceSource.DATASET_MAX


def _create_run(
    connection: sqlite3.Connection,
    key: tuple[str, dict[str, object], str, date, ReferenceSource, str],
    total: int,
) -> int:
    scoring_hash, scoring_config, metrics_hash, reference, source, fingerprint = key
    cursor = connection.execute(
        "INSERT INTO scoring_runs (status, scoring_hash, scoring_version, scoring_json, "
        "metrics_hash, reference_date, reference_date_source, input_fingerprint, "
        "conversations_total, tool_version, started_at) "
        "VALUES ('running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scoring_hash,
            SCORING_VERSION,
            canonical_json(scoring_config),
            metrics_hash,
            reference.isoformat(),
            source.value,
            fingerprint,
            total,
            __version__,
            utc_now(),
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _mark_used(connection: sqlite3.Connection, run_id: int) -> None:
    """Record that this run is the latest one created or reused; `export` defaults to it."""
    connection.execute(
        "UPDATE scoring_runs SET last_used_seq = "
        "(SELECT coalesce(max(last_used_seq), 0) + 1 FROM scoring_runs) WHERE id = ?",
        (run_id,),
    )


def _set_status(connection: sqlite3.Connection, run_id: int, status: str) -> None:
    with transaction(connection):
        connection.execute(
            "UPDATE scoring_runs SET status = ?, finished_at = ? WHERE id = ?",
            (status, utc_now() if status == "complete" else None, run_id),
        )


def _score_pending(
    connection: sqlite3.Connection,
    versions: Sequence[_Version],
    settings: Settings,
    result: RunResult,
    batching: tuple[Progress | None, int],
) -> None:
    progress, batch_size = batching
    done = {
        row[0]
        for row in connection.execute(
            "SELECT version_id FROM conversation_scores WHERE run_id = ?", (result.run_id,)
        )
    }
    pending = [v for v in versions if v.version_id not in done]
    scored = len(done)
    for batch in _chunks(pending, batch_size):
        with transaction(connection):
            raw = _raw_metrics(connection, batch, settings, result)
            connection.executemany(
                "INSERT INTO conversation_scores (run_id, version_id, frequency, reciprocity, "
                "initiative, recency, coverage, score, level, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _score_row(result.run_id, v.version_id, raw[v.version_id], settings)
                    for v in batch
                ),
            )
            scored += len(batch)
            connection.execute(
                "UPDATE scoring_runs SET conversations_scored = ? WHERE id = ?",
                (scored, result.run_id),
            )
        if progress:
            progress(scored, len(versions))


def _score_row(
    run_id: int, version_id: int, raw: RawMetrics, settings: Settings
) -> tuple[object, ...]:
    score = score_conversation(raw, settings.scoring)
    return (
        run_id,
        version_id,
        score.frequency,
        score.reciprocity,
        score.initiative,
        score.recency,
        score.coverage,
        score.score,
        score.level.value,
        score.reason.value,
    )


def _raw_metrics(
    connection: sqlite3.Connection,
    batch: Sequence[_Version],
    settings: Settings,
    result: RunResult,
) -> dict[int, RawMetrics]:
    """Stored raw metrics for the batch, computing (and storing) the missing ones."""
    ids = [v.version_id for v in batch]
    placeholders = ",".join("?" * len(ids))
    stored = {
        row["version_id"]: raw_from_row(row)
        for row in connection.execute(
            f"SELECT * FROM conversation_metrics WHERE metrics_hash = ? "
            f"AND version_id IN ({placeholders})",
            (result.metrics_hash, *ids),
        )
    }
    result.metrics_reused += len(stored)
    missing = [v for v in batch if v.version_id not in stored]
    if not missing:
        return stored

    groups = {v.version_id: v.is_group for v in missing}
    computed_at = utc_now()
    for version_id, messages in messages_of(connection, list(groups)):
        raw = compute_raw_metrics(
            messages, settings.metrics, result.reference_date, groups[version_id]
        )
        stored[version_id] = raw
        connection.execute(
            f"INSERT INTO conversation_metrics (version_id, metrics_hash, "
            f"{', '.join(RAW_COLUMNS)}, computed_at) "
            f"VALUES (?, ?, {', '.join('?' * len(RAW_COLUMNS))}, ?)",
            (version_id, result.metrics_hash, *raw_to_row(raw), computed_at),
        )
        result.metrics_computed += 1
    return stored


def messages_of(
    connection: sqlite3.Connection, version_ids: list[int]
) -> Iterator[tuple[int, list[StoredMessage]]]:
    placeholders = ",".join("?" * len(version_ids))
    rows = connection.execute(
        "SELECT version_id, seq, timestamp, sender, direction, text, message_type, media_kind "
        f"FROM messages WHERE version_id IN ({placeholders}) ORDER BY version_id, timestamp, seq",
        version_ids,
    ).fetchall()
    for version_id, group in groupby(rows, key=lambda row: row[0]):
        yield (
            version_id,
            [
                StoredMessage(
                    seq=row[1],
                    timestamp=datetime.fromisoformat(row[2]),
                    sender=row[3],
                    direction=Direction(row[4]) if row[4] else None,
                    text=row[5],
                    message_type=MessageType(row[6]),
                    media_kind=MediaKind(row[7]) if row[7] else None,
                )
                for row in group
            ],
        )


def raw_to_row(raw: RawMetrics) -> tuple[object, ...]:
    values: list[object] = []
    for name in RAW_COLUMNS:
        value = getattr(raw, name)
        if isinstance(value, bool):
            value = int(value)
        elif isinstance(value, datetime):
            value = format_timestamp(value)
        values.append(value)
    return tuple(values)


def raw_from_row(row: sqlite3.Row) -> RawMetrics:
    values = {name: row[name] for name in RAW_COLUMNS}
    values["is_group"] = bool(values["is_group"])
    if values["last_inbound_at"] is not None:
        values["last_inbound_at"] = datetime.fromisoformat(values["last_inbound_at"])
    return RawMetrics(**values)


def _chunks(items: Sequence[_Version], size: int) -> Iterator[Sequence[_Version]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
