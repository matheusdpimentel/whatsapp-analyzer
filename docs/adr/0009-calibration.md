# ADR 0009: Calibration against human-reviewed examples

- Status: Accepted
- Date: 2026-11-16

## Context

Milestone 8 must let people measure model, metric and policy quality against human judgement
and adjust prompts, criteria and policies on evidence, without new trust assumptions: offline,
no external calls, immutable and traceable inputs, privacy of message content, categorical
labels without invented confidence.

## Decision

- **A calibration set is a stored, immutable sample** (`calibration_sets`, `calibration_items`)
  of versions with a valid primary label, drawn by a seeded hash order. Strategy
  (`representative` or `prioritized`), seed, population, the primary, review and scoring runs
  are recorded. Human labels (`calibration_labels`) are immutable too; SQLite triggers refuse
  updates and deletes. Migration 5; the migration splitter now keeps trigger bodies whole.
- **Representative and prioritized samples are different things.** Only a representative sample
  yields intervals and a "generalizable" flag; a prioritized one is labeled biased everywhere.
- **No new weights.** The four existing policies are evaluated; `suggest` reports the
  error/workload frontier instead of ranking them.
- **Fixed scoring reference.** A set records its scoring run when drawn; reports never read the
  latest one. Immutability also covers sets, `INSERT OR REPLACE` and late inserts (triggers).
- **Overfitting guard.** `suggest` picks on a development part and re-measures on a held-out
  part chosen by a stable hash; it never writes configuration.
- **Reuse over rewrite.** Evidence comes from `consolidate` (independence rules, scoring run);
  levels reuse `classify`, extracted from `score_conversation` without behavior change.
- **Privacy.** The review sheet and every export carry ids and labels only. `show` prints one
  version's messages on the terminal, sanitized, restricted to versions of a set, and shows no
  model labels; with `--json` it omits senders and text. Notes and reviewer identity are not stored.
- **Gates.** Minimum label counts live in `[calibration]`; below them the command exits with
  code 10, `CALIBRATION_REQUIRED`.

## Consequences

- One human label per item: no inter-rater agreement yet.
- Statistics are implemented in a small module (Wilson, quadratic-weighted kappa, exact sign
  test) and tested against values worked out by hand; no new dependency.
- Knowledge Builder remains out of scope.
