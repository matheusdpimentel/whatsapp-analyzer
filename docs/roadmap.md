# Roadmap

| Milestone | Scope | Status |
|---|---|---|
| M0 | Project structure, CLI, interface languages | done |
| M1 | Synthetic data generator and canonical schema | done |
| M2 | Import, validation and normalization | done |
| M3 | SQLite workspace, deterministic metrics, scoring and exports | done |
| M4 | Offline LLM core: Provider Protocol, fake provider, prompts, masking, estimates | done |
| M5 | Persistent runner: budgets, rate limits, deduplication, checkpoints, reconciliation | done |
| M6 | Real providers (OpenAI, Anthropic, OpenAI-compatible), BYOK, authorization, agent guide | done |
| M7 | Independent review (single, selective, dual) and categorical consolidation ([review.md](review.md)) | done |
| M8 | Calibration with human-reviewed examples | planned |
| Future | Knowledge Builder (proposal below, not implemented) | proposed |

## M7: configurable reviewers

Implemented in milestone 7 as specified below; see [review.md](review.md) and ADR 0008.
Weighted consolidation is left to M8.

### Modes

1. **Single model** (the default, today's behavior). One model classifies each conversation.
   There is no reviewer. Simplest and cheapest.
2. **Selective review.** A second model reviews only selected cases, to spend where it
   matters. Candidate criteria:
   - the first label is `REVIEW_REQUIRED`;
   - ambiguity signals in the first answer (for example few or weak reasons);
   - disagreement between the deterministic metric level and the semantic label;
   - criteria defined by the user;
   - low confidence, **only** when a valid confidence measure exists. No confidence is
     invented from labels or token probabilities that the provider does not document.
3. **Independent double analysis.** Two models analyze the same conversations separately.
   The second model never receives the first model's classification before producing its own.
   Both results stay identified (model, provider, prompt, run) and traceable.

### Consolidation

A later step compares the results according to a policy chosen by the user:

- require agreement (disagreements stay unresolved);
- prefer one model (the other is informative);
- route disagreements to human review;
- apply configured weights.

Rules for consolidation:

- `HIGH`, `MEDIUM` and `LOW` are ordinal categories, not continuous measures: they are never
  averaged arithmetically.
- No confidence probability is made up.
- Weights have documented semantics (what a weight changes in the decision) and can be
  calibrated in M8 against examples reviewed by people.
- The original results of each model are always preserved; consolidation adds a result and
  never overwrites one.
- Different models may use different providers and credentials, each with its own
  authorization, budget and limits.

### Constraints carried over

- Each reviewer run is a normal M5 run: checkpoints, budgets, reuse and reconciliation apply
  unchanged, and sending to each provider needs its own authorization (M6).
- Selective review must be reproducible: the selection criteria and the inputs they used are
  recorded, so the same configuration selects the same cases.

## M8: calibration

Human-reviewed examples to measure agreement, calibrate thresholds and consolidation weights,
and report where models and metrics disagree. Exit code 10 (`CALIBRATION_REQUIRED`) is
reserved for it.

## Proposal: Knowledge Builder (not implemented)

An optional, later module that turns conversation histories into structured knowledge for
AI agents: facts, preferences and communication profiles (tone, preferred channels and
times, recurring topics), each item with traceable sources (conversation, version and
message ids), never free text detached from its evidence.

Principles, to be designed before any code:

- Opt-in per workspace, with the same per-invocation authorization as any provider call.
- Every extracted item cites its sources and carries a review status; nothing is used by an
  agent until a person accepts it (human review first).
- Privacy controls: masking, exclusion lists, retention limits, export and deletion of every
  item about a person; no sensitive or protected attributes inferred.
- Built on the existing runner, budgets and consolidation; no new hosting or backend.
