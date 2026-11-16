"""Calibration statistics against expected values worked out by hand (not by the code)."""

import pytest

from whatsapp_analyzer.calibration import stats


def test_wilson_interval_matches_the_published_value_for_8_of_10() -> None:
    low, high = stats.wilson(8, 10) or (0, 0)
    assert low == pytest.approx(0.4902, abs=1e-3)
    assert high == pytest.approx(0.9433, abs=1e-3)
    assert stats.wilson(0, 0) is None


def test_kappa_is_zero_when_agreement_is_only_what_chance_gives() -> None:
    # observed: LL, HH, LH, HL one quarter each; expected: every cell one quarter
    pairs = [("LOW", "LOW"), ("HIGH", "HIGH"), ("LOW", "HIGH"), ("HIGH", "LOW")]
    assert stats.weighted_kappa(pairs) == pytest.approx(0.0)


def test_kappa_penalizes_adjacent_disagreement_less_than_distant() -> None:
    # one MEDIUM predicted for a HIGH: 1 - 0.0625 / 0.3125 = 0.8 (worked out by hand)
    pairs = [("LOW", "LOW"), ("MEDIUM", "MEDIUM"), ("HIGH", "HIGH"), ("HIGH", "MEDIUM")]
    assert stats.weighted_kappa(pairs) == pytest.approx(0.8)
    far = [("LOW", "LOW"), ("MEDIUM", "MEDIUM"), ("HIGH", "HIGH"), ("HIGH", "LOW")]
    assert stats.weighted_kappa(far) is not None
    assert stats.weighted_kappa(far) < stats.weighted_kappa(pairs)  # type: ignore[operator]


def test_kappa_is_undefined_without_variation_or_pairs() -> None:
    assert stats.weighted_kappa([("LOW", "LOW")] * 5) is None
    assert stats.weighted_kappa([]) is None
    assert stats.weighted_kappa([("HIGH", "HIGH"), ("LOW", "LOW")]) == pytest.approx(1.0)


def test_sign_test_is_exact() -> None:
    assert stats.exact_sign_test(0, 5) == pytest.approx(2 / 32)
    assert stats.exact_sign_test(3, 3) == 1.0  # 2 * 42 / 64 is capped at 1
    assert stats.exact_sign_test(0, 0) is None


def test_confusion_and_distances() -> None:
    pairs = [("LOW", "LOW"), ("LOW", "MEDIUM"), ("HIGH", "LOW"), ("HIGH", "LOW")]
    matrix = stats.confusion(pairs)
    assert matrix["LOW"] == {"LOW": 1, "MEDIUM": 1, "HIGH": 0}
    assert matrix["HIGH"]["LOW"] == 2
    assert stats.distance_counts(pairs) == (1, 2)
