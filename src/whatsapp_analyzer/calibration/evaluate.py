"""Agreement report, run comparison and divergence triage against human labels.

Labels are ordinal categories (LOW < MEDIUM < HIGH). A rater that gives no ordinal label for
an item (failed, refused, REVIEW_REQUIRED, INSUFFICIENT_DATA...) is counted as `abstained`,
never as right or wrong, and its coverage is reported next to its accuracy. Nothing is
averaged, no confidence is invented, and a prioritized sample never yields a general
performance figure (no interval is given and the report says so).
"""

import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from whatsapp_analyzer.calibration import stats
from whatsapp_analyzer.calibration.common import (
    POLICIES,
    CalibrationError,
    Evidence,
    Item,
    RunJob,
    consolidated,
    divergence_kinds,
    evidence,
    get_run,
    is_partial,
    link_for,
    require_labels,
    run_description,
    run_jobs,
    sampling_caveats,
    set_items,
    usable,
)
from whatsapp_analyzer.config import CalibrationSettings
from whatsapp_analyzer.ingest.csv_export import escape_formula

REPORT_COLUMNS = (
    "rater",
    "kind",
    "run_id",
    "human_labeled",
    "compared",
    "abstained",
    "absent",
    "coverage",
    "accuracy",
    "ci95_low",
    "ci95_high",
    "weighted_kappa",
    "off_by_one",
    "off_by_two",
    "needs_human_review",
    "generalizable",
)


@dataclass
class Rater:
    name: str
    kind: str  # model | metrics | consolidated
    run_id: int | None
    labels: Mapping[int, str | None]  # version_id -> label; a missing key means absent
    needs_human: Mapping[int, bool] | None = None  # consolidated raters only


def _raters(
    connection: sqlite3.Connection,
    set_row: sqlite3.Row,
    extra_runs: Sequence[int],
    link: sqlite3.Row | None,
) -> tuple[list[Rater], dict[int, Evidence]]:
    facts = evidence(connection, set_row["primary_run_id"], link, set_row["scoring_run_id"])
    primary = get_run(connection, set_row["primary_run_id"])
    raters = [
        Rater(
            f"primary:{primary['provider']}/{primary['model']}",
            "model",
            primary["id"],
            {v: e.primary for v, e in facts.items()},
        )
    ]
    if link is not None:
        reviewer = get_run(connection, link["review_run_id"])
        raters.append(
            Rater(
                f"reviewer:{reviewer['provider']}/{reviewer['model']}",
                "model",
                reviewer["id"],
                {v: e.reviewer for v, e in facts.items() if e.reviewed},
            )
        )
    if set_row["scoring_run_id"] is not None:
        raters.append(
            Rater(
                f"metrics:scoring_run_{set_row['scoring_run_id']}",
                "metrics",
                None,
                {v: e.metric for v, e in facts.items()},
            )
        )
    for run_id in extra_runs:
        run = get_run(connection, run_id)
        raters.append(
            Rater(
                f"run:{run['id']}:{run['provider']}/{run['model']}",
                "model",
                run["id"],
                {v: j.label for v, j in run_jobs(connection, run_id).items()},
            )
        )
    if link is not None:
        for policy in POLICIES:
            by_version = consolidated(connection, link, policy)
            raters.append(
                Rater(
                    f"consolidated:{policy.value}",
                    "consolidated",
                    None,
                    {v: label for v, (label, _) in by_version.items()},
                    {v: human for v, (_, human) in by_version.items()},
                )
            )
    return raters, facts


def _score(rater: Rater, items: Sequence[Item], representative: bool) -> dict[str, object]:
    pairs: list[tuple[str, str]] = []
    abstained = absent = 0
    human_needed = 0
    for item in items:
        if item.version_id not in rater.labels:
            absent += 1
            continue
        label = rater.labels[item.version_id]
        if label in stats.ORDER:
            assert item.human is not None and label is not None
            pairs.append((item.human, label))
        else:
            abstained += 1
        if rater.needs_human is not None and rater.needs_human.get(item.version_id):
            human_needed += 1
    correct = sum(1 for h, p in pairs if h == p)
    one, two = stats.distance_counts(pairs)
    interval = stats.wilson(correct, len(pairs)) if representative else None
    return {
        "rater": rater.name,
        "kind": rater.kind,
        "run_id": rater.run_id,
        "human_labeled": len(items),
        "compared": len(pairs),
        "abstained": abstained,
        "absent": absent,
        "coverage": stats.rounded(len(pairs) / len(items)) if items else None,
        "accuracy": stats.rounded(correct / len(pairs)) if pairs else None,
        "ci95": None if interval is None else [stats.rounded(x) for x in interval],
        "weighted_kappa": stats.rounded(stats.weighted_kappa([(h, p) for h, p in pairs])),
        "off_by_one": one,
        "off_by_two": two,
        "needs_human_review": None if rater.needs_human is None else human_needed,
        "confusion": stats.confusion(pairs),
    }


def report(
    connection: sqlite3.Connection,
    set_row: sqlite3.Row,
    extra_runs: Sequence[int],
    settings: CalibrationSettings,
) -> tuple[list[list[str]], dict[str, object]]:
    items = set_items(connection, set_row["id"])
    require_labels(items, settings.min_labels_report, "report")
    labeled = usable(items)
    link = link_for(connection, set_row)
    raters, facts = _raters(connection, set_row, extra_runs, link)
    representative = set_row["sampling"] == "representative"
    scores = [_score(r, labeled, representative) for r in raters]
    runs = {r.run_id for r in raters if r.run_id is not None}
    partial = sorted(i for i in runs if is_partial(get_run(connection, i)))
    caveats = sampling_caveats(set_row, items, settings.small_sample_warning)
    if partial:
        caveats.append("partial_runs")
    rows = [
        [
            str(s["rater"]),
            str(s["kind"]),
            "" if s["run_id"] is None else str(s["run_id"]),
            *[
                "" if s[k] is None else str(s[k])
                for k in (
                    "human_labeled",
                    "compared",
                    "abstained",
                    "absent",
                    "coverage",
                    "accuracy",
                )
            ],
            *([str(x) for x in s["ci95"]] if isinstance(s["ci95"], list) else ["", ""]),
            "" if s["weighted_kappa"] is None else str(s["weighted_kappa"]),
            str(s["off_by_one"]),
            str(s["off_by_two"]),
            "" if s["needs_human_review"] is None else str(s["needs_human_review"]),
            str(representative).lower(),
        ]
        for s in scores
    ]
    summary: dict[str, object] = {
        "set": set_row["name"],
        "sampling": set_row["sampling"],
        "generalizable": representative,
        "population": set_row["population"],
        "items": len(items),
        "human_labeled": len(labeled),
        "caveats": caveats,
        "partial_runs": partial,
        "raters": scores,
        "model_pair": _model_pair(raters, labeled, facts, link),
        "runs": [run_description(connection, get_run(connection, i)) for i in sorted(runs)],
        "interpretation": (
            "Estimates for the population the sample was drawn from, with 95% Wilson intervals."
            if representative
            else "Prioritized sample: figures describe these cases only, not general performance."
        ),
    }
    return rows, summary


def _model_pair(
    raters: Sequence[Rater],
    items: Sequence[Item],
    facts: Mapping[int, Evidence],
    link: sqlite3.Row | None,
) -> dict[str, object] | None:
    """Primary vs independent reviewer on the labeled items both classified, and who the
    human sided with when they differ."""
    if link is None:
        return None
    pairs: list[tuple[str, str]] = []
    right = Counter[str]()
    for item in items:
        e = facts.get(item.version_id)
        if e is None or e.primary not in stats.ORDER or e.reviewer not in stats.ORDER:
            continue
        assert e.primary is not None and e.reviewer is not None
        pairs.append((e.primary, e.reviewer))
        if e.primary != e.reviewer:
            right[
                "primary"
                if item.human == e.primary
                else "reviewer"
                if item.human == e.reviewer
                else "neither"
            ] += 1
    agree = sum(1 for a, b in pairs if a == b)
    return {
        "items_both_labeled": len(pairs),
        "agreement": stats.rounded(agree / len(pairs)) if pairs else None,
        "weighted_kappa": stats.rounded(stats.weighted_kappa(pairs)),
        "when_they_disagree_human_sided_with": {
            "primary": right["primary"],
            "reviewer": right["reviewer"],
            "neither": right["neither"],
        },
    }


# Compare ---------------------------------------------------------------------------------

COMPARE_COLUMNS = (
    "conversation_key",
    "version_id",
    "data_version",
    "human_label",
    "a_status",
    "a_label",
    "b_status",
    "b_label",
    "pairing",
    "a_correct",
    "b_correct",
)


def _pairing(item: Item, a: RunJob | None, b: RunJob | None) -> str:
    """Why an item is (not) paired between two runs. A missing context hash proves nothing:
    two NULLs are not the same context."""
    if item.human not in stats.ORDER:
        return "no_human_label"
    if a is None or b is None:
        return "absent_in_a" if a is None else "absent_in_b"
    if a.context_hash is None or b.context_hash is None:
        return "context_unverified"
    if a.context_hash != b.context_hash:
        return "context_mismatch"
    if a.label not in stats.ORDER or b.label not in stats.ORDER:
        return "no_ordinal_label"
    return "paired"


def compare(
    connection: sqlite3.Connection,
    set_row: sqlite3.Row,
    run_a: int,
    run_b: int,
    settings: CalibrationSettings,
) -> tuple[list[list[str]], dict[str, object]]:
    """Two runs on the same conversations *and versions* of the set. Only versions both runs
    classified from the same prepared context, with an ordinal label in both and a human
    label, are paired; everything else is counted by reason, never dropped silently."""
    if run_a == run_b:
        raise CalibrationError("COMPARE_SAME_RUN", run_id=run_a)
    items = set_items(connection, set_row["id"])
    require_labels(items, settings.min_labels_report, "compare")
    runs = [get_run(connection, run_a), get_run(connection, run_b)]
    jobs = [run_jobs(connection, run_a), run_jobs(connection, run_b)]
    reasons: Counter[str] = Counter()
    rows: list[list[str]] = []
    paired: list[tuple[str, str, str]] = []
    prompt_differs = 0
    for item in items:
        a, b = jobs[0].get(item.version_id), jobs[1].get(item.version_id)
        pairing = _pairing(item, a, b)
        if pairing == "paired":
            assert item.human and a and b and a.label and b.label
            paired.append((item.human, a.label, b.label))
            prompt_differs += a.prompt_hash != b.prompt_hash
        reasons[pairing] += 1
        correct = [
            ""
            if j is None or item.human not in stats.ORDER or j.label not in stats.ORDER
            else str(j.label == item.human).lower()
            for j in (a, b)
        ]
        rows.append(
            [
                escape_formula(item.key),
                str(item.version_id),
                "current" if item.current else "superseded",
                item.human or "",
                a.status if a else "",
                (a.label or "") if a else "",
                b.status if b else "",
                (b.label or "") if b else "",
                pairing,
                *correct,
            ]
        )
    representative = set_row["sampling"] == "representative"
    only_a = sum(1 for h, a, b in paired if a == h and b != h)
    only_b = sum(1 for h, a, b in paired if b == h and a != h)
    per_run = []
    for index, name in enumerate(("a", "b")):
        pairs = [(h, (a, b)[index]) for h, a, b in paired]
        correct_count = sum(1 for h, p in pairs if h == p)
        interval = stats.wilson(correct_count, len(pairs)) if representative else None
        per_run.append(
            {
                "side": name,
                **run_description(connection, runs[index]),
                "accuracy_on_paired": stats.rounded(correct_count / len(pairs)) if pairs else None,
                "ci95": None if interval is None else [stats.rounded(x) for x in interval],
                "weighted_kappa": stats.rounded(stats.weighted_kappa(pairs)),
                "off_by_two": stats.distance_counts(pairs)[1],
            }
        )
    caveats = sampling_caveats(set_row, items, settings.small_sample_warning)
    if any(is_partial(r) for r in runs):
        caveats.append("partial_runs")
    if reasons["context_mismatch"]:
        caveats.append("context_mismatch")
    if reasons["context_unverified"]:
        caveats.append("context_unverified")
    if len(paired) < settings.min_labels_report:
        caveats.append("few_paired_items")
    summary: dict[str, object] = {
        "set": set_row["name"],
        "sampling": set_row["sampling"],
        "generalizable": representative,
        "items": len(items),
        "paired": len(paired),
        "excluded_by_reason": {k: v for k, v in sorted(reasons.items()) if k != "paired"},
        "paired_with_different_prompts": prompt_differs,
        "runs": per_run,
        "discordant": {"only_a_correct": only_a, "only_b_correct": only_b},
        "sign_test_p": stats.rounded(stats.exact_sign_test(only_a, only_b)),
        "caveats": caveats,
        "note": "Differences are descriptive; a small sign-test p-value is not proof when the "
        "prompt, model or sample were chosen after looking at these labels.",
    }
    return rows, summary


# Divergences -----------------------------------------------------------------------------

DIVERGENCE_COLUMNS = (
    "conversation_key",
    "version_id",
    "data_version",
    "kinds",
    "primary_label",
    "reviewer_label",
    "metric_level",
    "model_distance",
    "metric_distance",
    "in_calibration_set",
    "human_label",
)


def _distance(a: str | None, b: str | None) -> int | None:
    if a in stats.ORDER and b in stats.ORDER:
        assert a is not None and b is not None
        return abs(stats.ORDER[a] - stats.ORDER[b])
    return None


def disagreements(
    connection: sqlite3.Connection,
    primary_run: int,
    link: sqlite3.Row | None,
    set_row: sqlite3.Row | None,
    scoring_run: int | None,
) -> tuple[list[list[str]], dict[str, object]]:
    """Versions where models or the metrics diverge, widest gaps first. Metric levels come
    from `scoring_run`, which the summary reports."""
    facts = evidence(connection, primary_run, link, scoring_run)
    humans = (
        {} if set_row is None else {i.version_id: i for i in set_items(connection, set_row["id"])}
    )
    found = []
    for e in facts.values():
        kinds = divergence_kinds(e)
        if kinds:
            found.append(
                (e, kinds, _distance(e.primary, e.reviewer), _distance(e.primary, e.metric))
            )
    found.sort(key=lambda f: (-max(f[2] or 0, f[3] or 0), -len(f[1]), f[0].key, f[0].version_id))
    counts: Counter[str] = Counter(k for _, kinds, _, _ in found for k in kinds)
    rows = []
    for e, kinds, model_distance, metric_distance in found:
        item = humans.get(e.version_id)
        rows.append(
            [
                escape_formula(e.key),
                str(e.version_id),
                "current" if e.current else "superseded",
                ";".join(kinds),
                e.primary or "",
                e.reviewer or "",
                e.metric or "",
                "" if model_distance is None else str(model_distance),
                "" if metric_distance is None else str(metric_distance),
                "" if set_row is None else str(item is not None).lower(),
                "" if item is None or item.human is None else item.human,
            ]
        )
    summary: dict[str, object] = {
        "primary_run": primary_run,
        "review_run": None if link is None else link["review_run_id"],
        "metrics_scoring_run": scoring_run,
        "versions_with_a_label": sum(1 for e in facts.values() if e.primary is not None),
        "diverging": len(found),
        "by_kind": dict(sorted(counts.items())),
        "set": None if set_row is None else set_row["name"],
        "note": "Order: widest ordinal gap first. Divergence is a reason to look, not an error "
        "verdict; only human labels measure correctness.",
    }
    return rows, summary
