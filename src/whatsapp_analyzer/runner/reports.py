"""Status and export of LLM runs, each read from one WAL snapshot.

Reported and locally estimated token counts, known costs, in-flight reservations and the
conservative reservations of attempts with unknown billing are always reported apart, and an
unknown value stays `null`.
"""

import csv
import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from whatsapp_analyzer import __version__
from whatsapp_analyzer.fileio import atomic_write_files
from whatsapp_analyzer.ingest.csv_export import escape_formula
from whatsapp_analyzer.runner.ledger import load_ledger
from whatsapp_analyzer.runner.model import RESUMABLE, JobStatus, RunStatus
from whatsapp_analyzer.store.db import read_snapshot, utc_now

EXPORT_SCHEMA = 1
EXPORT_COLUMNS = (
    "conversation_key",
    "source_format",
    "content_sha256",
    "data_version",
    "run_id",
    "run_status",
    "run_partial",
    "job_status",
    "needs_reconciliation",
    "skip_reason",
    "execution_status",
    "error_kind",
    "validation_error",
    "label",
    "reasons",
    "evidence",
    "security_flags",
    "attempts",
    "last_billing",
    "reused_from_attempt",
    "input_tokens",
    "output_tokens",
    "usage_estimated",
    "cost",
)


class LlmRunNotFoundError(Exception):
    def __init__(self, run_id: int | None) -> None:
        super().__init__(str(run_id))
        self.run_id = run_id


@dataclass
class ExportResult:
    path: Path
    metadata_path: Path
    rows: int
    metadata: dict[str, object]


def latest_run(connection: sqlite3.Connection, run_id: int | None) -> sqlite3.Row:
    if run_id is None:
        row = connection.execute(
            "SELECT * FROM llm_runs ORDER BY last_used_seq DESC, id DESC LIMIT 1"
        ).fetchone()
    else:
        row = connection.execute("SELECT * FROM llm_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise LlmRunNotFoundError(run_id)
    return row  # type: ignore[no-any-return]


def run_report(connection: sqlite3.Connection, run_id: int | None = None) -> dict[str, object]:
    with read_snapshot(connection):
        run = latest_run(connection, run_id)
        return _report(connection, run)


def _report(connection: sqlite3.Connection, run: sqlite3.Row) -> dict[str, object]:
    run_id = run["id"]
    jobs = dict.fromkeys((s.value for s in JobStatus), 0)
    jobs.update(
        dict(
            connection.execute(
                "SELECT status, count(*) FROM llm_jobs WHERE run_id = ? GROUP BY status", (run_id,)
            ).fetchall()
        )
    )
    attempts = connection.execute(
        "SELECT a.* FROM llm_attempts a JOIN llm_jobs j ON j.id = a.job_id WHERE j.run_id = ?",
        (run_id,),
    ).fetchall()
    limits = json.loads(run["limits_json"])
    ledger = load_ledger(
        connection,
        run_id,
        limits.get("max_tokens"),
        None if limits.get("max_cost") is None else Decimal(limits["max_cost"]),
    )
    price = json.loads(run["price_json"]) if run["price_json"] else None
    costs = ledger.costs()
    return {
        "run": {
            "id": run_id,
            "status": run["status"],
            "stop_reason": run["stop_reason"],
            "partial": _is_partial(run, jobs),
            "provider": run["provider"],
            "model": run["model"],
            "prompt": f"{run['prompt_id']}@{run['prompt_version']}",
            "run_key": run["run_key"],
            "scope_fingerprint": run["scope_fingerprint"],
            "jobs_total": run["jobs_total"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "limits": limits,
            "price": price,
        },
        "jobs": jobs,
        "attempts": {
            "total": len(attempts),
            "by_billing": _count(a["billing"] for a in attempts),
            "by_error_kind": _count(a["error_kind"] for a in attempts if a["error_kind"]),
        },
        "usage": {  # billed attempts only: rejected or unsent ones consumed nothing
            "reported": _usage(
                [a for a in attempts if a["billing"] == "billed" and a["usage_estimated"] == 0]
            ),
            "estimated": _usage(
                [a for a in attempts if a["billing"] == "billed" and a["usage_estimated"] == 1]
            ),
        },
        "cost": _cost(attempts, price),
        "reservations": {
            "in_flight_tokens": ledger.inflight_tokens,
            "in_flight_cost": costs["in_flight"],
            "unknown_billing_tokens": ledger.unknown_tokens,
            "unknown_billing_cost": costs["unknown_billing"],
        },
        "budget": {
            "max_tokens": limits.get("max_tokens"),
            "max_cost": limits.get("max_cost"),
            "committed_tokens": (
                ledger.consumed_tokens + ledger.inflight_tokens + ledger.unknown_tokens
            ),
            "committed_cost": costs["committed"],
            "note": "reservations use local token estimates; a guide, not a guarantee for "
            "real providers",
        },
    }


def _is_partial(run: sqlite3.Row, jobs: dict[str, int]) -> bool:
    open_jobs = jobs.get("pending", 0) + jobs.get("in_flight", 0) + jobs.get("retry_wait", 0)
    return RunStatus(run["status"]) in RESUMABLE or open_jobs > 0


def _count(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _usage(attempts: list[sqlite3.Row]) -> dict[str, object]:
    """Sums over attempts whose counts are all known; the others are counted, not added."""
    buckets = ("input_uncached", "input_cache_read", "input_cache_write", "output_total")
    complete = [a for a in attempts if all(a[b] is not None for b in buckets)]
    reasoning = [a["output_reasoning"] for a in complete if a["output_reasoning"] is not None]
    return {
        "attempts": len(attempts),
        "attempts_with_unknown_counts": len(attempts) - len(complete),
        **{b: sum(a[b] for a in complete) if complete else None for b in buckets},
        "output_reasoning_reported": sum(reasoning) if reasoning else None,
    }


def _cost(attempts: list[sqlite3.Row], price: dict[str, object] | None) -> dict[str, object]:
    billed = [a for a in attempts if a["billing"] == "billed"]
    known = [Decimal(a["cost"]) for a in billed if a["cost"] is not None]
    return {
        "currency": price["currency"] if price else None,
        "known_total": str(sum(known, Decimal(0))) if known else None,
        "billed_attempts_without_cost": len(billed) - len(known),
        "estimated": any(a["usage_estimated"] == 1 for a in billed),
    }


# Export --------------------------------------------------------------------------------


def export_llm_run(
    connection: sqlite3.Connection,
    run_id: int | None,
    path: Path,
    on_snapshot: Callable[[], None] | None = None,
) -> ExportResult:
    with read_snapshot(connection):
        run = latest_run(connection, run_id)
        if on_snapshot:
            on_snapshot()  # test seam: concurrent commits must not show up below
        report = _report(connection, run)
        rows = connection.execute(
            "SELECT c.conversation_key, c.source_format, v.content_sha256, "
            "(c.current_version_id = v.id) AS is_current, j.*, "
            "(SELECT billing FROM llm_attempts a WHERE a.job_id = j.id ORDER BY number DESC "
            " LIMIT 1) AS last_billing "
            "FROM llm_jobs j JOIN conversation_versions v ON v.id = j.version_id "
            "JOIN conversations c ON c.id = v.conversation_id "
            "WHERE j.run_id = ? ORDER BY c.source_format, c.conversation_key, j.id",
            (run["id"],),
        ).fetchall()
        usage = {
            row["job_id"]: row
            for row in connection.execute(
                "SELECT a.job_id, count(*) AS n, "
                "sum(a.input_uncached + a.input_cache_read + a.input_cache_write) AS input, "
                "sum(a.output_total) AS output, max(a.usage_estimated) AS estimated, "
                "sum(CASE WHEN a.input_uncached IS NULL OR a.input_cache_read IS NULL "
                "OR a.input_cache_write IS NULL OR a.output_total IS NULL THEN 1 ELSE 0 END) "
                "AS unknown, sum(CASE WHEN a.cost IS NULL THEN 1 ELSE 0 END) AS no_cost, "
                "group_concat(a.cost, '|') AS costs "
                "FROM llm_attempts a JOIN llm_jobs j ON j.id = a.job_id "
                "WHERE j.run_id = ? AND a.billing = 'billed' GROUP BY a.job_id",
                (run["id"],),
            )
        }

    run_info = report["run"]
    assert isinstance(run_info, dict)
    partial = "true" if run_info["partial"] else "false"
    superseded = sum(1 for row in rows if not row["is_current"])
    reconcile = sum(1 for row in rows if row["status"] == JobStatus.AWAITING_RECONCILIATION)
    metadata: dict[str, object] = {
        "schema": EXPORT_SCHEMA,
        "exported_at": utc_now(),
        "tool_version": __version__,
        **report,
        "rows": len(rows),
        "superseded_rows": superseded,
        "awaiting_reconciliation_rows": reconcile,
        "content_included": False,
    }
    metadata_path = path.with_name(path.name + ".meta.json")
    atomic_write_files(
        (path, _lines(_row(row, run, partial, usage.get(row["id"])) for row in rows)),
        (metadata_path, [json.dumps(metadata, indent=2, sort_keys=True) + "\n"]),
    )
    return ExportResult(path, metadata_path, len(rows), metadata)


def _row(row: sqlite3.Row, run: sqlite3.Row, partial: str, usage: sqlite3.Row | None) -> list[str]:
    def cell(value: object) -> str:
        return "" if value is None else str(value)

    def joined(value: str | None) -> str:
        return "" if value is None else ";".join(json.loads(value))

    complete = usage is not None and usage["unknown"] == 0
    costs = [] if usage is None or usage["no_cost"] else usage["costs"].split("|")
    return [
        escape_formula(row["conversation_key"]),
        row["source_format"],
        row["content_sha256"],
        "current" if row["is_current"] else "superseded",
        str(run["id"]),
        run["status"],
        partial,
        row["status"],
        "true" if row["status"] == JobStatus.AWAITING_RECONCILIATION else "false",
        cell(row["skip_reason"]),
        cell(row["execution_status"]),
        cell(row["error_kind"]),
        cell(row["validation_error"]),
        cell(row["label"]),
        joined(row["reasons_json"]),
        joined(row["evidence_json"]),
        joined(row["security_flags_json"]),
        str(row["attempts"]),
        cell(row["last_billing"]),
        cell(row["reused_from_attempt"]),
        cell(usage["input"]) if complete and usage is not None else "",
        cell(usage["output"]) if complete and usage is not None else "",
        ("true" if usage["estimated"] else "false") if usage is not None else "",
        str(sum((Decimal(c) for c in costs), Decimal(0))) if costs else "",
    ]


def _lines(rows: Iterable[list[str]]) -> Iterable[str]:
    class _Line:
        value = ""

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
