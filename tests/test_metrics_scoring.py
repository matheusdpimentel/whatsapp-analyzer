"""Normalization, score and levels."""

import random
from dataclasses import replace

import pytest

from whatsapp_analyzer.config import ScoringSettings, Weights
from whatsapp_analyzer.metrics.raw import RawMetrics
from whatsapp_analyzer.metrics.scoring import Level, Reason, normalize, score_conversation

SETTINGS = ScoringSettings()


def raw(**overrides: object) -> RawMetrics:
    values: dict[str, object] = {
        "is_group": False,
        "messages_considered": 20,
        "inbound_messages": 10,
        "outbound_messages": 10,
        "inbound_content_messages": 10,
        "inbound_deleted_messages": 0,
        "excluded_system": 0,
        "excluded_after_reference": 0,
        "excluded_ignored_text": 0,
        "excluded_deleted": 0,
        "collapsed_duplicates": 0,
        "active_weeks": 4,
        "outbound_turns": 5,
        "answered_outbound_turns": 4,
        "pending_outbound_turns": 0,
        "sessions": 4,
        "contact_started_sessions": 2,
        "last_inbound_at": None,
        "days_since_last_inbound": 90,
    }
    values.update(overrides)
    return RawMetrics(**values)  # type: ignore[arg-type]


def test_normalized_values() -> None:
    components = normalize(raw(), SETTINGS)
    assert components == {
        "frequency": 0.5,  # 4 / 8
        "reciprocity": 0.8,  # 4 / 5
        "initiative": 0.5,  # 2 / 4
        "recency": 0.5,  # one half-life
    }


def test_weighted_score_and_level() -> None:
    result = score_conversation(raw(), SETTINGS)
    # 0.25*0.5 + 0.35*0.8 + 0.15*0.5 + 0.25*0.5 = 0.605
    assert result.score == 0.605
    assert result.coverage == 1.0
    assert (result.level, result.reason) == (Level.MEDIUM, Reason.SCORED)


def test_frequency_is_capped() -> None:
    assert normalize(raw(active_weeks=20), SETTINGS)["frequency"] == 1.0


def test_pending_turns_leave_the_denominator() -> None:
    components = normalize(
        raw(outbound_turns=5, answered_outbound_turns=2, pending_outbound_turns=1), SETTINGS
    )
    assert components["reciprocity"] == 0.5


def test_missing_components_are_renormalized() -> None:
    # Reciprocity empty (no decided outbound turn), initiative empty (1 session < 2).
    result = score_conversation(
        raw(outbound_turns=0, answered_outbound_turns=0, sessions=1, contact_started_sessions=1),
        SETTINGS,
    )
    assert result.reciprocity is None and result.initiative is None
    assert result.coverage == 0.5  # (0.25 + 0.25) / 1.0
    assert result.score == 0.5  # (0.25*0.5 + 0.25*0.5) / 0.5


def test_low_coverage_is_insufficient_data() -> None:
    result = score_conversation(
        raw(
            outbound_turns=0,
            answered_outbound_turns=0,
            sessions=1,
            days_since_last_inbound=None,
        ),
        SETTINGS,
    )
    assert result.coverage == 0.25
    assert (result.level, result.reason) == (Level.INSUFFICIENT_DATA, Reason.LOW_COVERAGE)
    assert result.score is None


def test_no_messages_is_insufficient_data() -> None:
    result = score_conversation(raw(messages_considered=0), SETTINGS)
    assert (result.level, result.reason) == (Level.INSUFFICIENT_DATA, Reason.NO_MESSAGES)


def test_groups_are_not_comparable() -> None:
    result = score_conversation(raw(is_group=True), SETTINGS)
    assert (result.level, result.reason) == (Level.NOT_COMPARABLE, Reason.GROUP)
    assert result.score is None and result.frequency is None


def test_high_requires_enough_inbound_content() -> None:
    strong = raw(
        active_weeks=8,
        answered_outbound_turns=5,
        contact_started_sessions=4,
        days_since_last_inbound=0,
    )
    assert score_conversation(strong, SETTINGS).level is Level.HIGH
    gated = score_conversation(replace(strong, inbound_content_messages=2), SETTINGS)
    assert (gated.level, gated.reason) == (Level.MEDIUM, Reason.HIGH_GATE)


def test_threshold_boundaries_are_inclusive() -> None:
    settings = replace(SETTINGS, weights=Weights(1, 0, 0, 0))
    assert (
        score_conversation(raw(active_weeks=8), replace(settings, high_threshold=1.0)).level
        is Level.HIGH
    )
    assert (
        score_conversation(raw(active_weeks=4), replace(settings, medium_threshold=0.5)).level
        is Level.MEDIUM
    )
    assert (
        score_conversation(raw(active_weeks=3), replace(settings, medium_threshold=0.5)).level
        is Level.LOW
    )


def test_zero_weight_component_does_not_count_for_coverage() -> None:
    settings = replace(SETTINGS, weights=Weights(0.5, 0, 0, 0.5))
    result = score_conversation(raw(outbound_turns=0, answered_outbound_turns=0), settings)
    assert result.coverage == 1.0
    assert result.score == 0.5


def test_scores_are_bounded_property() -> None:
    rng = random.Random(1234)
    for _ in range(2000):
        outbound = rng.randint(0, 30)
        pending = rng.randint(0, outbound)
        sessions = rng.randint(0, 30)
        weights = Weights(*(rng.choice([0, rng.random()]) for _ in range(4)))
        if sum(weights.__dict__.values()) == 0:
            continue
        settings = replace(
            SETTINGS,
            weights=weights,
            min_coverage=rng.uniform(0.01, 1),
            target_active_weeks=rng.randint(1, 26),
        )
        metrics = raw(
            active_weeks=rng.randint(0, 26),
            outbound_turns=outbound,
            pending_outbound_turns=pending,
            answered_outbound_turns=rng.randint(0, outbound - pending),
            sessions=sessions,
            contact_started_sessions=rng.randint(0, sessions),
            days_since_last_inbound=rng.choice([None, rng.randint(0, 2000)]),
        )
        result = score_conversation(metrics, settings)
        present = [
            v
            for v in (result.frequency, result.reciprocity, result.initiative, result.recency)
            if v is not None
        ]
        assert all(0 <= v <= 1 for v in present)
        assert result.coverage is not None and 0 <= result.coverage <= 1
        if result.score is not None:
            weighted = [
                v
                for name, v in zip(
                    ("frequency", "reciprocity", "initiative", "recency"),
                    (result.frequency, result.reciprocity, result.initiative, result.recency),
                    strict=True,
                )
                if v is not None and getattr(weights, name) > 0
            ]
            assert min(weighted) - 1e-4 <= result.score <= max(weighted) + 1e-4


@pytest.mark.parametrize("level", list(Level))
def test_levels_are_stable_identifiers(level: Level) -> None:
    assert level.value == level.name
