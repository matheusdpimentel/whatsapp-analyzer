# Independent review and consolidation

A second model can review the classifications of a primary run. Three modes, set in
`[llm.review]`:

| Mode | What the reviewer classifies |
|---|---|
| `single` (default) | nothing: only the primary model |
| `selective` | the conversations selected by explicit `criteria` (and `--conversation` keys) |
| `dual` | every conversation the primary run classified or tried to |

The reviewer gets exactly the same prepared conversation and prompt template as the primary
model and **never** the primary classification, so the two results are independent. Its
run is an ordinary run: checkpoints, resume, reuse, reconciliation, budgets, rate limits and
per-invocation authorization all apply ([runner.md](runner.md), [providers.md](providers.md)).

## Configuration

```toml
[llm]                      # the primary model
provider = "openai"
model = "gpt-6-luna"

[llm.review]
mode = "selective"         # single | selective | dual
criteria = ["review_required", "unlabeled", "metric_disagreement"]
policy = "human_review"    # require_agreement | prefer_primary | prefer_reviewer | human_review
provider = "anthropic"     # the reviewer: any provider, BYOK
model = "claude-haiku-5-5"

[llm.review.connection]
api_key_env = "ANTHROPIC_API_KEY"

[llm.review.limits]        # the reviewer's own budget (default: the primary limits)
max_cost = "1.00"
```

The reviewer accepts `provider`, `model`, `prompt`, `max_output_tokens`, `temperature`,
`[llm.review.connection]`, `[llm.review.options]` and `[llm.review.limits]` (each limit not
set there is inherited from `[llm.limits]`). It always uses
the primary `[llm.context]`.

Independence is enforced, not assumed:

- The reviewer must use another provider or model than the primary run
  (`REVIEWER_SAME_AS_PRIMARY` otherwise): the same model with other generation settings is
  not an independent analysis, and it could reuse the primary results.
- The reviewer must prepare exactly the conversations the primary run prepared: the current
  `[llm.context]` and `[metrics] ignored_texts` must match those of the primary run
  (`REVIEW_CONTEXT_MISMATCH` otherwise, before anything is planned).
- At consolidation, a reviewer result that was copied from the primary run, is the primary
  request, or was built from another context is marked `reviewer_independent = false` and
  never counts as agreement.

## Selection criteria (selective mode)

| Criterion | Selects a conversation when |
|---|---|
| `review_required` | the primary label is `REVIEW_REQUIRED` |
| `unlabeled` | the primary job has no valid label (failed, invalid, refused, awaiting reconciliation) |
| `metric_disagreement` | the primary label (HIGH, MEDIUM, LOW) differs from the deterministic metric level of the latest complete scoring run |

`--conversation KEY` adds conversations explicitly. `metric_disagreement` uses one scoring
run, the most recently used complete one, recorded with the review (`metrics_scoring_run`).

The selection is fixed once a review of a primary run has been linked: running `llm review`
again with the same mode, criteria and reviewer reuses that scope (`frozen_from_review_run`)
and resumes the same review run, even if metric levels or reconciliations changed since.
`--reselect` asks for a new selection explicitly (a new review run if the set differs); the
scope reused afterwards is that of the review run used last. Reviews made with
`--conversation` keys are never reused as a criteria selection. A review that selects
nothing plans nothing and fixes no scope. The reviewer uses its own configured prompt
template; only the prepared context must be identical. Model aliases and proxies are compared
by name: two names for the same model are not detected. There is no
"low confidence" criterion: the models report no valid confidence measure, and none is
invented.

## Commands

```bash
whatsapp-analyzer llm run -w ./ws                       # the primary run (must be finished)
whatsapp-analyzer llm review -w ./ws --authorize-send anthropic
whatsapp-analyzer llm consolidate -w ./ws --out consolidated.csv
```

- `llm review` reviews the latest run of the `[llm]` provider and model, or `--primary-run ID`.
  An unfinished primary run is refused (`PRIMARY_RUN_UNFINISHED`): its selection could change.
  `--authorize-send` names the **reviewer's** destination. Limits and other run options
  apply to the review run. Exit codes are those of `llm run`; with nothing selected it exits
  0 without planning anything.
- A review is linked to its primary run only once it may execute (authorized, if a real
  provider), so a refused review never becomes "the latest review".
- Each selection (mode, criteria, explicit keys) has its own link, even when two selections
  pick the same versions and therefore share a review run. Every `llm review` that plans or
  resumes a selection marks it as the review used last.
- `llm consolidate` compares the review used last (or `--primary-run` / `--review-run`) per
  conversation under the configured policy or `--policy`, and writes a CSV and its
  `.meta.json`. Neither run is changed; consolidation can be repeated with other policies.

## Consolidation

Labels are ordinal categories: they are never averaged, weighted or turned into a score.

| Case | `require_agreement` | `prefer_primary` | `prefer_reviewer` | `human_review` |
|---|---|---|---|---|
| not reviewed | primary label (`primary_only`) | same | same | same |
| both agree | that label (`agreed`) | same | same | same |
| they disagree | none (`unresolved`) | primary (`preferred_primary`) | reviewer (`preferred_reviewer`) | none (`needs_human_review`) |
| only one valid label | none (`unresolved`) | that label (`single_result`) | that label (`single_result`) | none (`needs_human_review`) |
| no valid label | none (`no_result`) | same | same | same |

`needs_human_review` is true whenever there is no consolidated label or it is
`REVIEW_REQUIRED`. Each row keeps both labels, reasons and job statuses, the deterministic
metric level (from the recorded scoring run), whether the reviewer result is independent, the
agreement and where the consolidated label came from. The metadata also reports the recorded
cost of each run separately (`costs.primary`, `costs.reviewer`); they are never added up. The metadata names
both runs (id, provider, model, run key), the mode, criteria and policy, and whether a run
was partial. No message text is exported.

Calibration against human-reviewed examples (agreement of each policy with people) is in
[calibration.md](calibration.md). No numeric weights exist: policies are categorical.
