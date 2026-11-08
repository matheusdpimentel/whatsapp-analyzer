# Engagement metrics

The analyzer computes four deterministic metrics per conversation: frequency, reciprocity,
initiative and recency. It then combines them into a score and a level. No LLM is involved.
The same data and the same configuration always give the same results, on every machine.

## What the metrics mean, and what they do not

The metrics describe **observable activity** in a conversation: when messages were exchanged
and in which direction. They do not measure or infer interest, sentiment, intent, affinity,
opinions or any personal or sensitive attribute, and they must not be presented as such.

A `LOW` level means "little observable exchange in this export", nothing more. The export may
be incomplete, the contact may use another channel, or the chat may have been cleared.

The deterministic score is stored separately from any future semantic classification (LLM
milestones). The two are never mixed in one value.

## Workflow

```bash
whatsapp-analyzer init ./analysis                       # new or empty directory only
whatsapp-analyzer import ./exports --workspace ./analysis
whatsapp-analyzer metrics run --workspace ./analysis    # raw metrics + scores (run 1)
# edit [scoring] in ./analysis/analyzer.toml, or pass --config other.toml
whatsapp-analyzer metrics run --workspace ./analysis    # re-scores only (run 2)
whatsapp-analyzer metrics compare 1 2 --workspace ./analysis
whatsapp-analyzer status --workspace ./analysis
whatsapp-analyzer export --workspace ./analysis --out scores.csv
```

The workspace is resolved in this order: the `--workspace` option, the `WCA_WORKSPACE`
environment variable, then the current directory. `import` stores data only when
`--workspace` is given explicitly.

## Definitions

### Reference date

Every metric is computed "as of" a reference date `R`. The end of the analysis window is
`T = R + 1 day at 00:00`, so the reference date itself is included.

- `reference_date = "dataset_max"` (default): `R` is the date of the latest non-system message
  among the current versions of all conversations in the workspace.
- `reference_date = "2026-01-31"`: a fixed date.
- `metrics run --reference-date 2026-01-31`: overrides the configuration for one run.

Dates must fall between the years 1900 and 9000. A TOML date-time is rejected: use a date only.

The resolved date and where it came from are stored with every run and printed by
`metrics run`, `status` and `export`. The system clock is never used. With `dataset_max`,
importing newer messages moves the date and therefore recomputes every conversation, because
recency changes for everyone. Use a fixed date when you want stable results across imports.

### Timestamps

Timestamps are naive local times, exactly as the exporter wrote them (offsets are rejected on
import). They are never converted between time zones, so all conversations in a workspace are
assumed to use the same clock. Weeks are ISO weeks of those local dates. Audit fields such as
`started_at` are UTC and are never used in calculations.

### Canonical sequence

Messages are ordered by `(timestamp, seq)`, where `seq` is the message's position in the source
export. Messages with equal timestamps, which are common in minute-precision exports, keep their
source order. That order is part of the conversation's content hash, so equal content always
gives the same sequence.

### Considered messages

The following are checked in this order. Each excluded message is counted in exactly one
column:

| Excluded | Column |
|---|---|
| System messages (no direction) | `excluded_system` |
| Messages after the end of the reference date | `excluded_after_reference` |
| Text messages whose whole text is in `ignored_texts` | `excluded_ignored_text` |
| Deleted messages, only when `count_deleted_as_activity = false` | `excluded_deleted` |
| Exact repeats, only when `duplicates = "collapse"` | `collapsed_duplicates` |

Everything else counts, including media, short replies such as "ok", and deleted messages
(by default). Message length is never used.

**Deleted messages** count as activity, because something was sent. They are never counted as
content: `inbound_content_messages`, which the `HIGH` gate uses, includes only inbound text and
media.

**Duplicates.** Imports always keep the original records. Duplicates are *not* neutral for
every metric. In a minute-precision export, a repeated inbound "ok" after an outbound message
creates an extra inbound turn, for example. So the analytical policy is explicit:

- `duplicates = "keep"` (default) counts every stored record;
- `duplicates = "collapse"` counts each exact repeat once: same timestamp, sender, direction,
  text, type and media kind, keeping the first in canonical order.

Changing the policy changes the metrics hash, and the metrics are computed again.

### Turns and sessions

- A **turn** is a maximal run of consecutive messages in the same direction.
- A **session** is a run of messages in which no gap reaches `session_gap_hours` (default 24).
  A gap of exactly 24 hours starts a new session.

### Raw metrics

These are stored per conversation version and metrics configuration in `conversation_metrics`.

| Metric | Raw values |
|---|---|
| Frequency | `active_weeks`: distinct ISO weeks with at least one inbound message in the last `lookback_weeks` × 7 days up to `R` |
| Reciprocity | `outbound_turns`; `answered_outbound_turns`: the next turn starts within `reply_window_hours` after the turn's last message (the boundary counts as answered); `pending_outbound_turns`: the last turn is outbound and unanswered, and its window has not closed before `T`. A window ending exactly at `T` counts as still open. |
| Initiative | `sessions`; `contact_started_sessions`: sessions whose first message is inbound |
| Recency | `last_inbound_at`; `days_since_last_inbound` = `R − date(last_inbound_at)` in days |

Also stored: message counts by direction, `inbound_content_messages`,
`inbound_deleted_messages` and every exclusion counter.

### Normalization and score

These are computed by `[scoring]`. Changing it never recomputes raw metrics.

| Component | Formula | Empty when |
|---|---|---|
| frequency | `min(active_weeks / target_active_weeks, 1)` | never |
| reciprocity | `answered / (outbound_turns − pending)` | the denominator is 0 |
| initiative | `contact_started_sessions / sessions` | `sessions < min_sessions_for_initiative` |
| recency | `0.5 ** (days_since_last_inbound / recency_half_life_days)` | no inbound message |

```
score    = Σ wₖ·xₖ / Σ wₖ        over the non-empty components
coverage = Σ wₖ(non-empty) / Σ wₖ(all)
```

Components are rounded to 6 decimals and the score to 4 before any comparison.

### Levels

| Level | Rule | `reason` |
|---|---|---|
| `NOT_COMPARABLE` | group conversation (see below) | `group_conversation` |
| `INSUFFICIENT_DATA` | no considered messages | `no_messages` |
| `INSUFFICIENT_DATA` | `coverage < min_coverage` | `low_coverage` |
| `HIGH` | `score ≥ high` and `inbound_content_messages ≥ high_min_inbound_content_messages` | `scored` |
| `MEDIUM` | `score ≥ high` but too few inbound content messages | `high_gate` |
| `MEDIUM` | `score ≥ medium` | `scored` |
| `LOW` | otherwise | `scored` |

### Group conversations

A conversation is a group when its inbound messages come from at least two distinct, non-empty
sender names. Groups are imported and get descriptive values: message counts, active weeks,
sessions and recency. Turn-based reciprocity and initiative are left empty, because a reply
from any member is not comparable to a reply in a one-to-one chat. Groups are classified
`NOT_COMPARABLE` and have no score. A specific methodology for groups would need its own
approval.

The detection depends on sender names. A group in which only one member ever wrote looks
individual, and exporters that leave senders empty cannot reveal groups.

## Configuration

`init` writes `analyzer.toml` with these defaults:

```toml
[metrics]                       # changing this recomputes raw metrics
reference_date = "dataset_max"  # or a fixed date: "2026-01-31"
lookback_weeks = 26             # 1..520
reply_window_hours = 168        # 1..8760
session_gap_hours = 24          # 1..8760
ignored_texts = ["(Waiting for this message)"]
duplicates = "keep"             # or "collapse"
count_deleted_as_activity = true

[scoring]                       # changing this only re-scores
target_active_weeks = 8         # 1..520, at most metrics.lookback_weeks
recency_half_life_days = 90     # 1..3650
min_sessions_for_initiative = 2
min_coverage = 0.5              # 0.01..1
high_min_inbound_content_messages = 3

[scoring.weights]               # 0..1 each, not all zero; they need not sum to 1
frequency = 0.25
reciprocity = 0.35
initiative = 0.15
recency = 0.25

[scoring.thresholds]            # 0 < medium < high <= 1
high = 0.66
medium = 0.33
```

Unknown keys, wrong types and out-of-range values are rejected with exit code 4. Nothing is
corrected silently. Missing keys take the defaults above.

## Storage, caching and history

The workspace database (`analyzer.db`, SQLite in WAL mode) keeps:

- `imports` and `sources`: every applied import, its options, its aggregate report, and each
  file's name, format, size and SHA-256;
- `conversations`: identity `(source_format, conversation_key)` and the current version;
- `conversation_versions`: immutable content versions, identified by a SHA-256 of their
  canonical messages in source order, and `messages` for each version;
- `metric_configs`: every metrics configuration used, with its resolved reference date;
- `conversation_metrics`: raw values per (version, metrics hash);
- `scoring_runs`: scoring configuration, metrics hash, input fingerprint (the current version of
  every conversation), status and counts;
- `conversation_scores`: normalized values, score, level and reason per (run, version).

Nothing is deleted. Re-importing a conversation with different content adds a version and
moves the current pointer. Earlier versions, and every metric and score computed from them,
remain. Old scores therefore keep pointing at the exact data they were computed from.

| Change | Effect |
|---|---|
| Same files imported again | Nothing is written |
| One conversation's content changes | New version; the next run computes raw metrics only for it |
| Content goes back to an earlier version | That version becomes current again (no new copy) |
| `[metrics]`, `METRICS_VERSION`, or the resolved reference date changes | New metrics hash; raw metrics are computed again, and old values are kept |
| `[scoring]` or `SCORING_VERSION` changes | New run from stored raw metrics |
| Same scoring hash, metrics hash and input fingerprint as a complete run | Reused (`--force` creates a new run) |
| Same key as an incomplete run | That run is resumed |

Hashes are SHA-256 of canonical JSON (sorted keys, fixed separators) together with a code
version constant. Integer and decimal spellings (`168` and `168.0`) hash the same.

### Identity and re-imports

- Formats are separate namespaces: a CSV chat titled "Alex" and a TXT file named `Alex.txt` are
  different conversations. `status` warns when the same key exists in several formats, because
  importing the same chats from two exporters counts them twice.
- One import may not contain the same conversation in two files. That file is rejected with
  `CONVERSATION_ID_CONFLICT` instead of being merged.
- A replacement must share at least one message with the current version: the same timestamp,
  direction, type and text. Sender names are ignored, so a renamed contact is still the same
  chat. Otherwise the import is refused with `CONVERSATION_REPLACEMENT_CONFLICT` and nothing is
  stored. A different chat with the same name, or a chat whose history was cleared, needs
  `--replace-unrelated`.
- `--drop-duplicates` cannot be combined with `--workspace`. Use `duplicates = "collapse"`
  instead, which keeps the records.

### Concurrency, interruptions and exports during a run

- One writer at a time: `import --workspace` and `metrics run` hold `analyzer.lock`, which the
  operating system releases if the process dies. A second writer fails with `WORKSPACE_BUSY`.
  `status`, `export` and `metrics compare` never write analysis data, and they can run at any
  time. Like every command, they bring an older database schema up to date when opening it.
- `metrics run` commits every 500 conversations. After Ctrl+C the run is marked `incomplete`
  and the command exits with code 130. If the process is killed, the run stays `running` until
  the next writer starts, which marks it `incomplete`. Running the same command again resumes
  the run without duplicating rows.
- `export` reads the run and all its rows inside one read transaction, which is a consistent
  WAL snapshot. Exporting a run that is still in progress gives the conversations scored so far,
  with `"partial": true` in the metadata.

## Export format

`export --out scores.csv` writes a CSV and `scores.csv.meta.json`. Both are written to a
temporary file and then moved into place.

By default, `export` writes the run that `metrics run` created, resumed or reused most
recently, preferring complete runs that match the current data. `--run ID` selects any run.

CSV columns: `conversation_key`, `source_format`, `content_sha256`, `data_version`
(`current`/`superseded`), `run_id`, `run_status`, `run_current_data` (`true`/`false`),
`reference_date`, `metrics_hash`, `scoring_hash`, every raw
metric, `frequency`, `reciprocity`, `initiative`, `recency`, `coverage`, `score`, `level` and
`reason`. Empty cells mean "not available". Keys that start with `=`, `+`, `-` or `@` are
escaped against spreadsheet formula injection, as in the normalized CSV.

The metadata lists the run (status, hashes, input fingerprint, full metrics and scoring
configuration, reference date and its source), `partial`, `current_data` (whether the run
still matches the current data) and `superseded_rows`. A run computed before the data changed
is exported with `current_data: false`. Every row then carries `run_current_data = false`, and
rows computed from an earlier conversation version are also marked `superseded`. The run status
and currency are repeated on each row, so a CSV shared without its metadata still says whether
its results are partial or historical.

`status` and `metrics compare` show aggregate counts only, never conversation keys, senders or
text. The exported CSV contains conversation keys, which are often contact names. Keep it as
private as the original exports.

## Performance

This was measured on an Apple M4 with macOS 15.7, Python 3.11.16 and SQLite 3.53.1, using a
synthetic canonical CSV of 1,000,000 messages in 20,000 conversations (73 MB):

| Step | Time | Peak memory |
|---|---:|---:|
| `import` without a workspace (validation only, for comparison) | 10.4 s | 141 MB |
| `import --workspace` (first) | 28.1 s | 145 MB |
| `import --workspace` (same file again, nothing written) | 22.6 s | 146 MB |
| `metrics run` (all raw metrics + scores) | 6.0 s | 66 MB |
| `metrics run` with changed `[scoring]` (re-score only) | 0.8 s | 51 MB |
| `metrics run` with nothing changed (reused) | 0.5 s | 52 MB |
| `export` (20,000 rows) | 0.8 s | 72 MB |
| `metrics compare` | 0.2 s | — |

The database was 137 MB. Storing adds about 18 s per million messages to validation: staging,
hashing every conversation and inserting messages. An unchanged re-import still stages and
hashes everything before it concludes that nothing changed. Both are candidates for later
optimization.

## Limitations

- Superseded versions keep their messages, so the database grows with every changed
  re-import. No pruning command exists yet.
- Group detection relies on sender names (see above). For WhatsApp TXT exports, a misspelled
  `--owner` turns the owner's messages into inbound messages from a second sender, so one-to-one
  chats are classified as groups. The import report warns with `OWNER_NOT_IN_FILE`.
- The same chat imported from two exporters becomes two conversations (see Identity).
- `dataset_max` recomputes everything when newer messages arrive; use a fixed date for stable
  caching.
- Importing into a workspace is about 2.7 times slower than validation alone (see
  Performance).
- Frequency counts partial ISO weeks at both ends of the lookback window as whole weeks.

## Future improvements (out of scope for milestone 3)

1. A retention policy and a safe cleanup command for superseded conversation versions.
2. Faster detection of unchanged input files (for example by file hash before parsing), while
   keeping the per-conversation content hash as the source of truth.
3. Explicit handling of exports recorded in different time zones.
4. Validation of the writer lock on Windows (`msvcrt`) in CI and on real machines.
5. Detection of likely duplicates when the same chat is imported from different exporters.
