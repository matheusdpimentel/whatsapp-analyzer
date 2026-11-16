# ADR 0008: Independent review and consolidation

- Status: Accepted
- Date: 2026-11-15

## Context

Milestone 7 lets a second model review the primary classifications (selectively or for
every conversation) and consolidates both under a user policy, without new trust
assumptions: original results preserved and traceable, M5/M6 financial and authorization
guarantees intact, no invented confidence and no arithmetic on categories.

## Decision

- **A review is an ordinary run.** `llm review` builds the reviewer's run from
  `[llm.review]` (its own provider, model, key, options and limits; the primary
  `[llm.context]`) and runs it through the same code path as `llm run`, scoped to a fixed
  set of version ids taken from a finished primary run (`OpenRequest.versions`). Everything
  of M5/M6 applies unchanged; no second runner, budget or consent mechanism.
- **Independence by construction.** The reviewer's requests are built from the same
  prepared context and template; the primary output is never part of them.
- **Explicit, deterministic selection** (`review_required`, `unlabeled`,
  `metric_disagreement`, `--conversation`), only from finished primary runs. No confidence
  criterion.
- **Migration 4** adds `llm_reviews` (primary run, review run, mode, criteria, policy,
  selection size). Runs and their results are never modified.
- **Consolidation is derived** on demand (`llm consolidate`) from both runs' stored labels and
  a categorical policy (`require_agreement`, `prefer_primary`, `prefer_reviewer`,
  `human_review`), exported as CSV with metadata. It is recomputable with any policy.

## Consequences

- A review costs a second set of requests, governed by its own budget and authorization.
- The default primary lookup matches the `[llm]` provider and model; runs of a fake scenario or
  of another configuration are reviewed with `--primary-run`.
- Weighted consolidation and calibration are left to milestone 8.
