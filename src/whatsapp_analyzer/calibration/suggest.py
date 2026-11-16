"""Exploratory suggestions: which consolidation policy and which score thresholds fit the
human labels best. Read-only: nothing is applied and no configuration is written.

Choosing an option and judging it on the same labels overstates how good it is, so the labeled
items are split deterministically into a development part (where options are compared and the
best threshold pair is picked) and a held-out validation part (where the pick is only
re-measured). Policies are not ranked: they trade errors against human workload, so the
Pareto frontier is reported and the choice is left to the person. No weights are introduced.
"""

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import replace

from whatsapp_analyzer.calibration import stats
from whatsapp_analyzer.calibration.common import (
    POLICIES,
    Item,
    consolidated,
    link_for,
    require_labels,
    sampling_caveats,
    set_items,
    usable,
)
from whatsapp_analyzer.config import CalibrationSettings, ScoringSettings
from whatsapp_analyzer.metrics.scoring import Level, classify

MIN_VALIDATION = 10  # items in the held-out part
MIN_COMPARABLE = 10  # comparable predictions an option needs there to be judged
HIGH_GRID = tuple(round(0.50 + 0.05 * i, 2) for i in range(7))  # 0.50 .. 0.80
MEDIUM_GRID = tuple(round(0.20 + 0.05 * i, 2) for i in range(7))  # 0.20 .. 0.50
COLUMNS = (
    "kind",
    "split",
    "option",
    "items",
    "compared",
    "correct",
    "accuracy",
    "wrong",
    "needs_human_review",
    "on_frontier",
    "is_current_setting",
)


def split(items: Sequence[Item], seed: int, fraction: float) -> tuple[list[Item], list[Item]]:
    """(development, validation): a stable hash decides, so the same set splits the same way
    and later labels never move an item between parts."""
    development: list[Item] = []
    validation: list[Item] = []
    for item in items:
        digest = hashlib.sha256(f"{seed}:split:{item.version_id}".encode()).digest()
        (
            validation if int.from_bytes(digest[:8], "big") / 2**64 < fraction else development
        ).append(item)
    return development, validation


def suggest(
    connection: sqlite3.Connection, set_row: sqlite3.Row, settings: CalibrationSettings
) -> tuple[list[list[str]], dict[str, object]]:
    items = set_items(connection, set_row["id"])
    require_labels(items, settings.min_labels_suggest, "suggest")
    development, validation = split(usable(items), set_row["seed"], settings.validation_fraction)
    parts = {"development": development, "validation": validation}
    rows: list[list[str]] = []
    summary: dict[str, object] = {
        "set": set_row["name"],
        "sampling": set_row["sampling"],
        "exploratory": True,
        "development_items": len(development),
        "validation_items": len(validation),
        "applied": False,
    }
    caveats = sampling_caveats(set_row, items, settings.small_sample_warning)
    caveats.append("exploratory_suggestions")
    if len(validation) < MIN_VALIDATION:
        caveats.append("validation_too_small")
    summary["caveats"] = caveats

    link = link_for(connection, set_row)
    if link is None:
        summary["policies"] = None
        summary["policies_note"] = "No review is linked to the set's primary run."
    else:
        table = {p: consolidated(connection, link, p) for p in POLICIES}
        measured = {
            split_name: {p.value: _policy_row(table[p], part) for p in POLICIES}
            for split_name, part in parts.items()
        }
        frontier = {name: _frontier(measured[name]) for name in parts}
        for split_name in parts:
            for policy, m in measured[split_name].items():
                rows.append(
                    [
                        "policy",
                        split_name,
                        policy,
                        str(m["items"]),
                        str(m["compared"]),
                        str(m["correct"]),
                        _fmt(m["accuracy"]),
                        str(m["wrong"]),
                        str(m["needs_human_review"]),
                        str(policy in frontier[split_name]).lower(),
                        str(policy == link["policy"]).lower(),
                    ]
                )
        summary["policies"] = measured
        summary["frontier"] = {k: sorted(v) for k, v in frontier.items()}
        unvalidated = sorted(
            p for p, m in measured["validation"].items() if _count(m, "compared") < MIN_COMPARABLE
        )
        summary["policies_unvalidated"] = unvalidated  # too few comparable labels held out
        if unvalidated:
            caveats.append("validation_few_comparable")
        summary["frontier_in_both_parts"] = sorted(
            (set(frontier["development"]) & set(frontier["validation"])) - set(unvalidated)
        )
        summary["policy_note"] = (
            "Policies trade wrong labels against human workload; none is ranked first and no "
            "weight is applied. The frontier lists the non-dominated ones."
        )

    thresholds = _thresholds(connection, set_row, parts, rows)
    summary["thresholds"] = thresholds
    if (
        thresholds is not None
        and min(_validation_comparable(thresholds).values()) < MIN_COMPARABLE
        and "validation_few_comparable" not in caveats
    ):
        caveats.append("validation_few_comparable")
    return rows, summary


def _policy_row(
    by_version: dict[int, tuple[str | None, bool]], part: Sequence[Item]
) -> dict[str, object]:
    compared = correct = wrong = human = 0
    for item in part:
        label, needs = by_version.get(item.version_id, (None, True))
        human += needs
        if label in stats.ORDER:
            compared += 1
            ok = label == item.human
            correct += ok
            wrong += not ok
    return {
        "items": len(part),
        "compared": compared,
        "correct": correct,
        "wrong": wrong,
        "accuracy": stats.rounded(correct / compared) if compared else None,
        "needs_human_review": human,
    }


def _frontier(measured: dict[str, dict[str, object]]) -> set[str]:
    """Policies no other policy beats on both wrong labels and human workload."""
    cost = {
        name: (int(m["wrong"]), int(m["needs_human_review"]))  # type: ignore[call-overload]
        for name, m in measured.items()
    }
    return {
        name
        for name, (wrong, human) in cost.items()
        if not any(
            (w <= wrong and h <= human) and (w, h) != (wrong, human) for w, h in cost.values()
        )
    }


def _thresholds(
    connection: sqlite3.Connection,
    set_row: sqlite3.Row,
    parts: dict[str, list[Item]],
    rows: list[list[str]],
) -> dict[str, object] | None:
    run_id = set_row["scoring_run_id"]
    if run_id is None:
        return None
    run = connection.execute(
        "SELECT scoring_json, metrics_hash FROM scoring_runs WHERE id = ?", (run_id,)
    ).fetchone()
    config = json.loads(run["scoring_json"])
    current = replace(
        ScoringSettings(),
        high_threshold=config["high_threshold"],
        medium_threshold=config["medium_threshold"],
        high_min_inbound_content_messages=config["high_min_inbound_content_messages"],
    )
    stored = {
        r[0]: (r[1], r[2], r[3])
        for r in connection.execute(
            "SELECT s.version_id, s.level, s.score, m.inbound_content_messages "
            "FROM conversation_scores s JOIN conversation_metrics m "
            "ON m.version_id = s.version_id AND m.metrics_hash = ? WHERE s.run_id = ?",
            (run["metrics_hash"], run_id),
        )
    }

    def level(version: int, high: float, medium: float) -> str | None:
        found = stored.get(version)
        if (
            found is None
            or found[1] is None
            or found[0] in (Level.INSUFFICIENT_DATA, Level.NOT_COMPARABLE)
        ):
            return None
        return classify(
            found[1], found[2], replace(current, high_threshold=high, medium_threshold=medium)
        )[0].value

    def measure(part: Sequence[Item], high: float, medium: float) -> dict[str, object]:
        pairs = [
            (i.human, lv)
            for i in part
            if (lv := level(i.version_id, high, medium)) is not None and i.human is not None
        ]
        correct = sum(1 for h, p in pairs if h == p)
        return {
            "items": len(part),
            "compared": len(pairs),
            "correct": correct,
            "accuracy": stats.rounded(correct / len(pairs)) if pairs else None,
            "wrong": len(pairs) - correct,
        }

    grid = [(h, m) for h in HIGH_GRID for m in MEDIUM_GRID if m < h]
    now = (current.high_threshold, current.medium_threshold)
    results = {(h, m): {name: measure(part, h, m) for name, part in parts.items()} for h, m in grid}
    if now not in results:
        results[now] = {name: measure(part, *now) for name, part in parts.items()}

    def best_key(pair: tuple[float, float]) -> tuple[float, float, float]:
        accuracy = _accuracy(results[pair]["development"])
        # highest development accuracy; ties go to the pair closest to the current setting
        return (
            -(-1.0 if accuracy is None else accuracy),
            abs(pair[0] - now[0]) + abs(pair[1] - now[1]),
            pair[0],
        )

    best = min(results, key=best_key)
    for pair, by_part in sorted(results.items()):
        for name, m in by_part.items():
            rows.append(
                [
                    "threshold",
                    name,
                    f"high={pair[0]};medium={pair[1]}",
                    str(m["items"]),
                    str(m["compared"]),
                    str(m["correct"]),
                    _fmt(m["accuracy"]),
                    str(m["wrong"]),
                    "",
                    str(pair == best).lower(),
                    str(pair == now).lower(),
                ]
            )
    comparable = {
        "best": _count(results[best]["validation"], "compared"),
        "current": _count(results[now]["validation"], "compared"),
    }
    supported = best != now and improvement_holds(
        _accuracy(results[best]["validation"]),
        _accuracy(results[now]["validation"]),
        min(comparable.values()),
        len(parts["validation"]),
    )
    return {
        "scoring_run": run_id,
        "current": {"high": now[0], "medium": now[1], "measured": results[now]},
        "best_on_development": {"high": best[0], "medium": best[1], "measured": results[best]},
        "improvement_holds_on_validation": supported,
        "validation_comparable": comparable,
        "note": "The pair is picked on the development part only; the validation part just "
        "re-measures it. Treat it as a hypothesis to test on new labels, not a result.",
    }


def _fmt(value: object) -> str:
    return "" if value is None else str(value)


def _accuracy(measured: dict[str, object]) -> float | None:
    value = measured["accuracy"]
    return value if isinstance(value, float) else None


def improvement_holds(
    best: float | None, current: float | None, comparable: int, validation_items: int
) -> bool:
    """A threshold pair counts as validated only if it beats the current one on the held-out
    part AND enough held-out predictions were actually comparable (not abstentions)."""
    return (
        best is not None
        and current is not None
        and best > current
        and comparable >= MIN_COMPARABLE
        and validation_items >= MIN_VALIDATION
    )


def _count(measured: dict[str, object], key: str) -> int:
    value = measured[key]
    assert isinstance(value, int)
    return value


def _validation_comparable(thresholds: dict[str, object]) -> dict[str, int]:
    value = thresholds["validation_comparable"]
    assert isinstance(value, dict)
    return value
