"""Independent review and consolidation (milestone 7).

A review is an ordinary run (checkpoints, budgets, reuse, reconciliation and authorization
all apply unchanged) of a second model over versions selected from a finished primary run:

- `dual`: every conversation the primary run classified (or tried to);
- `selective`: only those matched by explicit criteria (`ReviewCriterion`) or keys.

The reviewer receives exactly the same prepared conversation as the primary model (checked:
same context settings), its own configured prompt template, and never the primary
classification; it must be another provider or model, so the two results are independent.
`llm_reviews` records the link; the runs themselves are never modified.

Consolidation is derived on demand, per primary job, from the two runs' stored labels and a
policy. Labels are ordinal categories: they are never averaged, and no confidence is made up.
"""

import csv
import io
import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from whatsapp_analyzer import __version__
from whatsapp_analyzer.config import ConsolidationPolicy, ReviewCriterion, ReviewMode
from whatsapp_analyzer.fileio import atomic_write_files
from whatsapp_analyzer.ingest.csv_export import escape_formula
from whatsapp_analyzer.runner.model import RESUMABLE, RunStatus
from whatsapp_analyzer.store.db import transaction, utc_now

CONSOLIDATION_SCHEMA = 1
_LABELED = ("succeeded", "reused")
_ORDINAL = {"LOW", "MEDIUM", "HIGH"}  # labels comparable with deterministic metric levels


class ReviewError(Exception):
    """`code` is a stable identifier for the CLI."""

    def __init__(self, code: str, **details: object) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class Selection:
    version_ids: tuple[int, ...]
    by_reason: dict[str, int]  # how many versions each criterion (or "conversation") matched
    scoring_run: int | None = None  # the metric levels used by `metric_disagreement`
    frozen_from: int | None = None  # the earlier review run whose scope is reused


def previous_selection(
    connection: sqlite3.Connection,
    primary_run_id: int,
    mode: ReviewMode,
    criteria: Sequence[ReviewCriterion],
    reviewer_run_key: str,
) -> Selection | None:
    """The scope of the earlier review of the same primary run, mode, criteria (no explicit
    keys) and reviewer that was used last. Reviewing again reuses it, so later metric runs or
    reconciliations never change the selected set silently (`--reselect` asks for a new
    selection explicitly)."""
    link = connection.execute(
        "SELECT l.review_run_id, l.criteria_json FROM llm_reviews l "
        "JOIN llm_runs r ON r.id = l.review_run_id "
        "WHERE l.primary_run_id = ? AND l.mode = ? AND l.selection_key = ? AND r.run_key = ? "
        "ORDER BY l.used_seq DESC LIMIT 1",
        (primary_run_id, mode.value, selection_key(mode, criteria, ()), reviewer_run_key),
    ).fetchone()
    if link is None:
        return None
    stored = json.loads(link[1])
    versions = tuple(
        r[0]
        for r in connection.execute(
            "SELECT version_id FROM llm_jobs WHERE run_id = ? ORDER BY version_id", (link[0],)
        )
    )
    return Selection(versions, {"frozen": len(versions)}, stored.get("scoring_run"), link[0])


def selection_key(
    mode: ReviewMode, criteria: Sequence[ReviewCriterion], keys: Sequence[str]
) -> str:
    """What defines a selection, independent of its result: mode, criteria and keys."""
    return json.dumps(
        {
            "mode": mode.value,
            "criteria": _criteria_names(mode, criteria),
            "keys": sorted(set(keys)),
        },
        sort_keys=True,
    )


def _criteria_names(mode: ReviewMode, criteria: Sequence[ReviewCriterion]) -> list[str]:
    return [c.value for c in criteria] if mode is ReviewMode.SELECTIVE else []


def primary_run(
    connection: sqlite3.Connection, provider: str, model: str, run_id: int | None
) -> sqlite3.Row:
    """The finished run to review: `run_id`, or the most recently used run of the primary
    provider and model that is not itself a review run. Finding it needs no API key."""
    if run_id is not None:
        row: sqlite3.Row | None = connection.execute(
            "SELECT * FROM llm_runs WHERE id = ?", (run_id,)
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT * FROM llm_runs WHERE (provider = ? OR provider LIKE ? ESCAPE '\\') "
            "AND model = ? AND id NOT IN (SELECT review_run_id FROM llm_reviews) "
            "ORDER BY last_used_seq DESC, id DESC LIMIT 1",
            (provider, _like_prefix(provider) + "@%", model),
        ).fetchone()
    if row is None:
        raise ReviewError("PRIMARY_RUN_NOT_FOUND", run_id=run_id)
    if connection.execute(
        "SELECT 1 FROM llm_reviews WHERE review_run_id = ?", (row["id"],)
    ).fetchone():
        raise ReviewError("PRIMARY_IS_A_REVIEW", run_id=row["id"])
    if RunStatus(row["status"]) in RESUMABLE or row["status"] == RunStatus.RUNNING:
        # a selection from an unfinished run would change when it finishes
        raise ReviewError("PRIMARY_RUN_UNFINISHED", run_id=row["id"], status=row["status"])
    return row


def _like_prefix(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def select(
    connection: sqlite3.Connection,
    primary_run_id: int,
    mode: ReviewMode,
    criteria: Sequence[ReviewCriterion],
    keys: Sequence[str] = (),
) -> Selection:
    """Deterministic: the same primary run, mode, criteria and keys give the same versions."""
    rows = connection.execute(
        "SELECT j.version_id, j.status, j.label, c.conversation_key FROM llm_jobs j "
        "JOIN conversation_versions v ON v.id = j.version_id "
        "JOIN conversations c ON c.id = v.conversation_id "
        "WHERE j.run_id = ? AND j.status != 'skipped' ORDER BY j.version_id",
        (primary_run_id,),
    ).fetchall()
    if mode is ReviewMode.DUAL:
        return Selection(tuple(r[0] for r in rows), {"all": len(rows)})
    scoring_run = None
    levels: dict[int, str] = {}
    if ReviewCriterion.METRIC_DISAGREEMENT in criteria:
        scoring_run = reference_scoring_run(connection)
        levels = metric_levels(connection, [r[0] for r in rows], scoring_run)
    wanted = set(keys)
    chosen: list[int] = []
    counts: Counter[str] = Counter()
    for version_id, status, label, key in rows:
        reasons = []
        if ReviewCriterion.REVIEW_REQUIRED in criteria and label == "REVIEW_REQUIRED":
            reasons.append(ReviewCriterion.REVIEW_REQUIRED.value)
        if ReviewCriterion.UNLABELED in criteria and (status not in _LABELED or label is None):
            reasons.append(ReviewCriterion.UNLABELED.value)
        level = levels.get(version_id)
        if (
            ReviewCriterion.METRIC_DISAGREEMENT in criteria
            and label in _ORDINAL
            and level in _ORDINAL
            and label != level
        ):
            reasons.append(ReviewCriterion.METRIC_DISAGREEMENT.value)
        if key in wanted:
            reasons.append("conversation")
        if reasons:
            chosen.append(version_id)
            counts.update(reasons)
    return Selection(tuple(chosen), dict(counts), scoring_run)


def reference_scoring_run(connection: sqlite3.Connection) -> int | None:
    """The most recently used complete scoring run (M3): one run, recorded with the review,
    so the levels behind a selection can always be found again."""
    row = connection.execute(
        "SELECT id FROM scoring_runs WHERE status = 'complete' "
        "ORDER BY last_used_seq DESC, id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else int(row[0])


def metric_levels(
    connection: sqlite3.Connection, version_ids: Sequence[int], scoring_run: int | None
) -> dict[int, str]:
    """The deterministic level of each version in `scoring_run`. Versions it did not score
    are absent."""
    if not version_ids or scoring_run is None:
        return {}
    marks = ",".join("?" * len(version_ids))
    return {
        int(version_id): str(level)
        for version_id, level in connection.execute(
            "SELECT version_id, level FROM conversation_scores "
            f"WHERE run_id = ? AND version_id IN ({marks})",
            (scoring_run, *version_ids),
        )
    }


def record(  # noqa: PLR0913, PLR0917
    connection: sqlite3.Connection,
    primary_run_id: int,
    review_run_id: int,
    mode: ReviewMode,
    criteria: Sequence[ReviewCriterion],
    policy: ConsolidationPolicy,
    selection: Selection,
    keys: Sequence[str] = (),
) -> None:
    """Record (or refresh) the link of one selection, and mark it as the review used last."""
    with transaction(connection):
        used = connection.execute(
            "SELECT coalesce(max(used_seq), 0) + 1 FROM llm_reviews"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO llm_reviews (primary_run_id, review_run_id, mode, selection_key, "
            "criteria_json, policy, selected, created_at, used_seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (primary_run_id, review_run_id, mode, selection_key) DO UPDATE SET "
            "criteria_json = excluded.criteria_json, policy = excluded.policy, "
            "selected = excluded.selected, used_seq = excluded.used_seq",
            (
                primary_run_id,
                review_run_id,
                mode.value,
                selection_key(mode, criteria, keys),
                json.dumps(
                    {
                        "criteria": _criteria_names(mode, criteria),
                        "scoring_run": selection.scoring_run,
                        "keys": sorted(set(keys)),
                    },
                    sort_keys=True,
                ),
                policy.value,
                len(selection.version_ids),
                utc_now(),
                used,
            ),
        )


def latest_review(
    connection: sqlite3.Connection, primary_run_id: int | None, review_run_id: int | None
) -> sqlite3.Row:
    query = "SELECT * FROM llm_reviews"
    clauses, params = [], []
    if primary_run_id is not None:
        clauses.append("primary_run_id = ?")
        params.append(primary_run_id)
    if review_run_id is not None:
        clauses.append("review_run_id = ?")
        params.append(review_run_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    row: sqlite3.Row | None = connection.execute(
        query + " ORDER BY used_seq DESC LIMIT 1", tuple(params)
    ).fetchone()
    if row is None:
        raise ReviewError("REVIEW_NOT_FOUND", primary_run=primary_run_id, review_run=review_run_id)
    return row


# Consolidation ---------------------------------------------------------------------------

COLUMNS = (
    "conversation_key",
    "version_id",
    "data_version",
    "metric_level",
    "reviewed",
    "primary_status",
    "primary_label",
    "primary_reasons",
    "reviewer_status",
    "reviewer_label",
    "reviewer_reasons",
    "reviewer_independent",
    "agreement",
    "policy",
    "consolidation",
    "consolidated_label",
    "consolidated_from",
    "needs_human_review",
)


@dataclass(frozen=True)
class Outcome:
    agreement: str  # agree | disagree | incomplete | not_reviewed
    consolidation: str
    label: str | None
    source: str | None  # primary | reviewer | both | None


def decide(  # noqa: PLR0911  (one return per documented case)
    primary: str | None, reviewer: str | None, reviewed: bool, policy: ConsolidationPolicy
) -> Outcome:
    """One conversation. `primary` / `reviewer` are valid labels or None (no valid result)."""
    if not reviewed:
        if primary is None:
            return Outcome("not_reviewed", "no_result", None, None)
        return Outcome("not_reviewed", "primary_only", primary, "primary")
    if primary is not None and reviewer is not None:
        if primary == reviewer:
            return Outcome("agree", "agreed", primary, "both")
        return _disagreement(primary, reviewer, policy)
    if primary is None and reviewer is None:
        return Outcome("incomplete", "no_result", None, None)
    # only one model produced a valid label
    if (
        policy is ConsolidationPolicy.PREFER_PRIMARY
        or policy is ConsolidationPolicy.PREFER_REVIEWER
    ):
        source = "primary" if primary is not None else "reviewer"
        return Outcome("incomplete", "single_result", primary or reviewer, source)
    if policy is ConsolidationPolicy.HUMAN_REVIEW:
        return Outcome("incomplete", "needs_human_review", None, None)
    return Outcome("incomplete", "unresolved", None, None)


def _disagreement(primary: str, reviewer: str, policy: ConsolidationPolicy) -> Outcome:
    if policy is ConsolidationPolicy.PREFER_PRIMARY:
        return Outcome("disagree", "preferred_primary", primary, "primary")
    if policy is ConsolidationPolicy.PREFER_REVIEWER:
        return Outcome("disagree", "preferred_reviewer", reviewer, "reviewer")
    if policy is ConsolidationPolicy.HUMAN_REVIEW:
        return Outcome("disagree", "needs_human_review", None, None)
    return Outcome("disagree", "unresolved", None, None)


def needs_human(outcome: Outcome) -> bool:
    return outcome.label is None or outcome.label == "REVIEW_REQUIRED"


@dataclass(frozen=True)
class Consolidated:
    rows: list[list[str]]
    summary: dict[str, object]


def consolidate(
    connection: sqlite3.Connection, review: sqlite3.Row, policy: ConsolidationPolicy
) -> Consolidated:
    primary_id, review_id = review["primary_run_id"], review["review_run_id"]
    primary_jobs = _jobs(connection, primary_id)
    reviewer_jobs = {row["version_id"]: row for row in _jobs(connection, review_id)}
    criteria = json.loads(review["criteria_json"])
    levels = metric_levels(
        connection, [row["version_id"] for row in primary_jobs], criteria.get("scoring_run")
    )
    primary_attempts = {
        r[0]
        for r in connection.execute(
            "SELECT a.id FROM llm_attempts a JOIN llm_jobs j ON j.id = a.job_id WHERE j.run_id = ?",
            (primary_id,),
        )
    }
    rows: list[list[str]] = []
    counts: Counter[str] = Counter()
    human = dependent = 0
    for job in primary_jobs:
        other = reviewer_jobs.get(job["version_id"])
        # only a labeled reviewer result can be (in)dependent; a skipped or failed one has none
        independent = (
            other is None or _label(other) is None or _independent(job, other, primary_attempts)
        )
        dependent += not independent
        # a result that is not an independent analysis never counts as the reviewer's label
        reviewer_label = _label(other) if independent else None
        outcome = decide(_label(job), reviewer_label, other is not None, policy)
        counts[outcome.consolidation] += 1
        human += needs_human(outcome)
        rows.append(
            [
                escape_formula(job["conversation_key"]),
                str(job["version_id"]),
                "current" if job["is_current"] else "superseded",
                levels.get(job["version_id"], ""),
                "true" if other is not None else "false",
                job["status"],
                job["label"] or "",
                _reasons(job),
                other["status"] if other is not None else "",
                (other["label"] or "") if other is not None else "",
                _reasons(other),
                "" if other is None or _label(other) is None else str(independent).lower(),
                outcome.agreement,
                policy.value,
                outcome.consolidation,
                outcome.label or "",
                outcome.source or "",
                "true" if needs_human(outcome) else "false",
            ]
        )
    runs = {
        r["id"]: r
        for r in connection.execute(
            "SELECT id, status, provider, model, run_key FROM llm_runs WHERE id IN (?, ?)",
            (primary_id, review_id),
        )
    }
    reviewed = sum(1 for row in rows if row[4] == "true")
    costs = {
        "primary": _known_cost(connection, primary_id),
        "reviewer": _known_cost(connection, review_id),
    }
    summary: dict[str, object] = {
        "primary_run": _run_info(runs[primary_id]),
        "review_run": _run_info(runs[review_id]),
        "mode": review["mode"],
        "criteria": criteria.get("criteria", []),
        "metrics_scoring_run": criteria.get("scoring_run"),
        "not_independent": dependent,
        "policy": policy.value,
        "rows": len(rows),
        "reviewed": reviewed,
        "by_consolidation": dict(sorted(counts.items())),
        "needs_human_review": human,
        "costs": costs,  # recorded costs of each run, never added together
        "partial": any(
            RunStatus(runs[i]["status"]) in RESUMABLE or runs[i]["status"] == RunStatus.RUNNING
            for i in (primary_id, review_id)
        ),
    }
    return Consolidated(rows, summary)


def export(result: Consolidated, path: Path) -> Path:
    metadata = {
        "schema": CONSOLIDATION_SCHEMA,
        "exported_at": utc_now(),
        "tool_version": __version__,
        **result.summary,
        "content_included": False,
        "note": "Labels are categories from each model; consolidation never averages them "
        "and reports no confidence. Original run results are unchanged.",
    }
    metadata_path = path.with_name(path.name + ".meta.json")
    atomic_write_files(
        (path, _lines([list(COLUMNS), *result.rows])),
        (metadata_path, [json.dumps(metadata, indent=2, sort_keys=True) + "\n"]),
    )
    return metadata_path


def _jobs(connection: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT j.*, c.conversation_key, (c.current_version_id = v.id) AS is_current "
        "FROM llm_jobs j JOIN conversation_versions v ON v.id = j.version_id "
        "JOIN conversations c ON c.id = v.conversation_id "
        "WHERE j.run_id = ? ORDER BY c.source_format, c.conversation_key, j.version_id",
        (run_id,),
    ).fetchall()


def _independent(primary: sqlite3.Row, reviewer: sqlite3.Row, primary_attempts: set[int]) -> bool:
    """The reviewer's result is a separate analysis of the same prepared conversation: not
    copied from the primary run, not the primary request, and built from the same context."""
    return (
        reviewer["reused_from_attempt"] not in primary_attempts
        and reviewer["request_id"] != primary["request_id"]
        and reviewer["context_hash"] == primary["context_hash"]
    )


def _known_cost(connection: sqlite3.Connection, run_id: int) -> str | None:
    """Recorded cost of a run's billed attempts; None if any of them has no known cost."""
    rows = connection.execute(
        "SELECT a.cost FROM llm_attempts a JOIN llm_jobs j ON j.id = a.job_id "
        "WHERE j.run_id = ? AND a.billing = 'billed'",
        (run_id,),
    ).fetchall()
    if any(r[0] is None for r in rows):
        return None
    return str(sum((Decimal(r[0]) for r in rows), Decimal(0)))


def _label(job: sqlite3.Row | None) -> str | None:
    if job is None or job["status"] not in _LABELED:
        return None
    label: str | None = job["label"]
    return label


def _reasons(job: sqlite3.Row | None) -> str:
    if job is None or not job["reasons_json"]:
        return ""
    return ";".join(json.loads(job["reasons_json"]))


def _run_info(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "status": row["status"],
        "provider": row["provider"],
        "model": row["model"],
        "run_key": row["run_key"],
    }


def _lines(rows: Iterable[list[str]]) -> Iterable[str]:
    for row in rows:
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="\n").writerow(row)
        yield buffer.getvalue()
