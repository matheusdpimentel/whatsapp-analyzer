# ADR 0004: SQLite persistence and engagement metrics

- Status: Accepted
- Date: 2026-11-07

## Context

Milestone 3 adds deterministic engagement metrics: frequency, reciprocity, initiative and
recency, weighted 0.25, 0.35, 0.15 and 0.25, with the levels HIGH, MEDIUM, LOW and
INSUFFICIENT_DATA. Results must be reproducible and traceable. Weights and thresholds must
change without recomputing raw metrics. Configurations must be comparable. Unchanged
conversations must not be reprocessed. Finished results must be exportable while processing
continues. No LLM may be used, and every test must use synthetic data.

## Decision

- **Workspace**: a new or empty directory with `analyzer.toml`, `analyzer.db` (SQLite, WAL,
  `foreign_keys`, `application_id`), a lock file and a `.gitignore` containing `*`. The
  directory gets mode 0700 and the database mode 0600. Schema migrations are tracked with
  `PRAGMA user_version`; each runs in one transaction, and newer databases are refused.
- **Immutable conversation versions** keep history intact. A conversation's identity is
  `(source_format, conversation_key)`. Content is hashed over its canonical messages in source
  order. New content becomes a new version, and the current pointer moves to it. Nothing is
  deleted or cascaded, so earlier metrics and scores stay attached to the data they were
  computed from. Exports label such rows `superseded`, and a run whose input fingerprint no
  longer matches is reported as `current_data: false`.
- **Identity safeguards**: formats are separate namespaces, and one import cannot hold the same
  conversation in two files. A replacement must share at least one message (timestamp,
  direction, type, text) with the current version unless `--replace-unrelated` is given. Imports
  are staged in a temporary table and applied in one transaction only when every file is
  accepted.
- **Raw metrics separate from scores**: `conversation_metrics` is keyed by (version, metrics
  hash), and `conversation_scores` by (run, version). A run is identified by scoring hash,
  metrics hash and input fingerprint, so a run is reused, resumed or created, never silently
  overwritten.
- **Reproducibility**: the reference date is resolved (`dataset_max`, configured, or
  `--reference-date`), included in the metrics hash, and stored with its source. The clock is
  used only for audit fields. Messages follow the canonical sequence `(timestamp, seq)`.
  Components and scores are rounded before comparison.
- **Duplicates**: records are always preserved. The analytical policy (`keep`/`collapse`) is
  an explicit, hashed setting, because duplicates change turn-based metrics.
- **Deleted messages** count as activity by default (configurable). They are excluded from the
  inbound content count that the HIGH gate uses.
- **Groups** get descriptive raw values. Turn-based reciprocity and initiative are left empty,
  and groups get a fifth level, `NOT_COMPARABLE`, until a group methodology is approved.
- **Batches and snapshots**: runs commit every 500 conversations under a single-writer OS lock.
  Readers use one read transaction (a WAL snapshot), so exports during a run are consistent and
  marked `partial`. Each exported row repeats the run status and whether the run matches the
  current data. Interrupted runs are marked `incomplete` and resumed.
- **No new dependencies**: `sqlite3`, `tomllib`, and validation with dataclasses.

## Consequences

- Changed re-imports grow the database, because superseded versions keep their messages.
  Pruning is future work.
- With `dataset_max`, newer data moves the reference date and invalidates all raw metrics, by
  design. A fixed date keeps the cache stable.
- The same chat from two exporters (iMazing TXT and CSV, for example) is stored twice. `status`
  warns about keys present in several formats.
- Group detection depends on sender names and can miss groups when exporters omit senders.
- The metrics describe observable activity only. Semantic classification (LLM milestones) will
  be stored separately and never mixed into the deterministic score.

## Future improvements

Version retention and cleanup, faster detection of unchanged files (keeping hash verification),
explicit time-zone handling, validation of the Windows writer lock, and detection of the same
chat imported from different exporters. See docs/metrics.md.
