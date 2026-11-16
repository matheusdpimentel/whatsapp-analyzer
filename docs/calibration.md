# Calibration against human-reviewed examples

`whatsapp-analyzer calibrate` measures how far the model labels, the deterministic metric
level and the consolidation policies agree with people, and shows where they diverge. It is
**offline and read-only over results**: no provider is called, no run, review or score is
changed, and no configuration is written. It only adds calibration sets (immutable) to the
workspace.

Labels stay ordinal categories (`LOW < MEDIUM < HIGH`). Nothing is averaged, no confidence is
invented and no weights are introduced.

## Workflow

```bash
# 1. Draw a sample of conversations the primary run classified, and a sheet to label.
whatsapp-analyzer calibrate sample -w ./ws --set week1 --size 60 --seed 7 --out week1.csv

# 2. Read each version locally and fill `human_label` (HIGH, MEDIUM, LOW or UNCLEAR).
whatsapp-analyzer calibrate show -w ./ws --set week1 --version 123

# 3. Import the filled sheet (all or nothing; stored labels cannot change).
whatsapp-analyzer calibrate import -w ./ws --set week1 --labels week1_filled.csv

# 4. Measure.
whatsapp-analyzer calibrate report        -w ./ws --set week1 --out report.csv
whatsapp-analyzer calibrate disagreements -w ./ws --out diverging.csv
whatsapp-analyzer calibrate compare       -w ./ws --set week1 --run 3 --run 5 --out cmp.csv
whatsapp-analyzer calibrate suggest       -w ./ws --set week1 --out suggestions.csv
```

`sample` and `import` write to the workspace; the rest only read it. If `sample` created the set
but could not write the sheet (full disk, permissions), the set exists: write the sheet again
with `calibrate sheet --set NAME --out FILE` (it never changes the set); do not draw it again. Every export is a CSV plus
a `.meta.json` and never contains message text.

## Sampling: representative or prioritized

| `--strategy` | Draw | Figures mean |
|---|---|---|
| `representative` (default) | seeded simple random sample of the versions with a valid primary label | an estimate for that population; the report adds 95% Wilson intervals |
| `prioritized` | versions where models or metrics diverge first, then random ones | **only these cases**; no interval is given and the report says it is not general performance |

The draw is deterministic: the same seed and workspace give the same sample. A set records its
strategy, seed, population, size, the primary and review runs and the scoring run, and it can
never be changed (SQLite triggers refuse updates, deletes, `INSERT OR REPLACE` and late
inserts on sets, items and labels; this protects the tool's own writes, not someone editing the
database file directly). The **scoring run** is fixed when the set is drawn (the one recorded
with the review, else the latest complete one at that moment) and every later report uses that
run, even if newer scoring runs exist. Sampling more is a new set under another name. A prioritized sample is the
right tool to find errors and train reviewers; it is the wrong tool to quote an accuracy.

## The review sheet and `show`

The sheet holds `set, conversation_key, version_id, data_version, human_label` and nothing
else: no message text and no model labels, so reviewers stay blind to them. Use `calibrate
show --set NAME --version ID` (or `--key KEY`) to print that version's messages on the
terminal (`--offset` and `--limit` page long ones). `show` works only for versions in the set,
writes nothing to disk, and replaces control and bidirectional characters so a message cannot
rewrite your terminal. With `--json` it returns only ids, times, directions and types
(`content_included: false`): senders and message text are never put in machine-readable output.

`human_label` accepts exactly `HIGH`, `MEDIUM`, `LOW` or `UNCLEAR` (a person could not
decide); blank means not labeled yet. Importing is all or nothing and fails on an invalid
label, an unknown or edited id or key, a duplicate row or a label that differs from one
already stored. There is no silent correction.

## `report`

Per rater, on the items that have a `HIGH`/`MEDIUM`/`LOW` human label: the primary model, the
reviewer, the deterministic metric level of the set's scoring run, every `--run` you add and
each of the four consolidation policies.

- `compared`: items where the rater gave an ordinal label. `abstained`: it gave none (failed,
  refused, `REVIEW_REQUIRED`, `INSUFFICIENT_DATA`, a policy that left it unresolved).
  `absent`: its run never planned that version. Abstentions are never counted as errors;
  `coverage = compared / labeled` sits next to `accuracy` so abstaining cannot hide.
- `accuracy`, `weighted_kappa` (quadratic-weighted, 1 is perfect, 0 is chance level, empty
  when undefined), `off_by_one` and `off_by_two` (how far the wrong labels are),
  the 3x3 confusion matrix (in the metadata) and, for policies, `needs_human_review`.
- `model_pair`: primary against the independent reviewer, and which of them the human sided
  with when they differed.

Caveats are listed in the output and the metadata: `prioritized_sample_not_generalizable`,
`small_sample` (fewer than `small_sample_warning` labels), `pending_labels`,
`superseded_versions`, `unclear_labels_excluded` and `partial_runs`.

## `compare`

Two runs (`--run A --run B`) on the same conversations **and versions** of a set. An item is
paired only when it has a human label, both runs planned that version, both classified it from
the same prepared context (`context_hash`) and both gave an ordinal label. A missing hash proves
nothing, so those items are not paired. Everything else is counted by reason (`absent_in_a`,
`absent_in_b`, `context_mismatch`, `context_unverified`, `no_ordinal_label`, `no_human_label`) and listed per item in the CSV. Runs that differ in prompt are expected (that
is the point); the number of pairs with different prompts is reported. The summary shows each
run's accuracy on the paired items, the recorded cost of each run (never added), the number of
items only A or only B got right and an exact sign-test p-value. Treat it as descriptive: it is
not evidence if the prompt or the sample were tuned on these same labels.

## `disagreements`

Versions where the two models differ, one result is missing, a label is `REVIEW_REQUIRED` or
the model label differs from the metric level, widest ordinal gap first. Without `--set` it
works on the latest primary run and its latest review; with `--set` it uses the set's runs and
adds `in_calibration_set` and `human_label`. Divergence is a reason to look, not a verdict.

## `suggest` (exploratory)

Which consolidation policy and which score thresholds fit the human labels. Because choosing an
option and judging it on the same labels overstates it, the labeled items are split
deterministically (stable hash, `validation_fraction` of them held out):

- **Policies** are measured on both parts. They trade wrong labels against human workload, so
  they are **not ranked and no weight is applied**: the output is the frontier of policies no
  other policy beats on both, in each part and in both.
- **Thresholds**: a grid of `high` (0.50 to 0.80) and `medium` (0.20 to 0.50) applied to the
  stored scores with the real level rules. The best pair is picked on the development part
  only (ties go to the pair closest to the current one); the held-out part just re-measures it,
  and `improvement_holds_on_validation` says whether it still beats the current setting there. It
is only `true` with at least 10 held-out items **and** at least 10 comparable predictions for
both pairs (abstentions do not count); otherwise the `validation_few_comparable` caveat is
added, and policies with fewer than 10 comparable held-out predictions are listed under
`policies_unvalidated` and left out of the stable frontier.

Suggestions never change `analyzer.toml`; apply one yourself, run `metrics run`, and calibrate
again with a **new set** of labels. `applied` is always `false` and `exploratory` always `true`.

## Configuration

```toml
[calibration]
min_labels_report = 30      # report and compare; below it, exit code 10
min_labels_suggest = 50     # suggest; below it, exit code 10
small_sample_warning = 100  # fewer labels than this adds the small_sample caveat
validation_fraction = 0.3   # share of labels held out by suggest (0.1 to 0.5)
```

Only `HIGH`/`MEDIUM`/`LOW` labels count toward the minimum. Exit code 10
(`CALIBRATION_REQUIRED`) carries `command`, `have` and `need` in `error.details`.

## Limits

- Model, metric and policy labels are compared with one human label per item; there is no
  inter-rater agreement between people (a future extension) and the importer stores no
  reviewer identity or notes.
- A sample of tens of items gives wide intervals: read the interval, not the point value.
- Conversation versions are immutable: labels belong to a version, and a newer import of the
  same conversation is a different item (`superseded` marks the old ones).
- Groups and conversations without a valid primary label are not in the population.
