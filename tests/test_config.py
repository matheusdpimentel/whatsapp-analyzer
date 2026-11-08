"""Workspace configuration: defaults, strict validation and stable hashes."""

import tomllib
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from whatsapp_analyzer.config import (
    DEFAULT_CONFIG_TEXT,
    METRICS_VERSION,
    ConfigError,
    DuplicatePolicy,
    Settings,
    load_settings,
    parse_settings,
)
from whatsapp_analyzer.hashing import config_hash


def test_default_file_matches_default_settings() -> None:
    assert parse_settings(tomllib.loads(DEFAULT_CONFIG_TEXT)) == Settings()


def test_empty_file_uses_defaults() -> None:
    assert parse_settings({}) == Settings()


def test_defaults_match_the_approved_weights_and_thresholds() -> None:
    scoring = Settings().scoring
    assert (
        scoring.weights.frequency,
        scoring.weights.reciprocity,
        scoring.weights.initiative,
        scoring.weights.recency,
    ) == (0.25, 0.35, 0.15, 0.25)
    assert (scoring.high_threshold, scoring.medium_threshold) == (0.66, 0.33)


def test_fixed_reference_date_and_choices() -> None:
    settings = parse_settings(
        {"metrics": {"reference_date": "2026-03-09", "duplicates": "collapse"}}
    )
    assert settings.metrics.reference_date == date(2026, 3, 9)
    assert settings.metrics.duplicates is DuplicatePolicy.COLLAPSE
    bare = parse_settings(tomllib.loads("[metrics]\nreference_date = 2026-03-09\n"))
    assert bare.metrics.reference_date == date(2026, 3, 9)


@pytest.mark.parametrize(
    ("data", "kind", "key"),
    [
        ({"extra": {}}, "unknown_key", "extra"),
        ({"metrics": {"lookback": 4}}, "unknown_key", "metrics.lookback"),
        ({"scoring": {"weights": {"speed": 1}}}, "unknown_key", "scoring.weights.speed"),
        ({"metrics": []}, "wrong_type", "metrics"),
        ({"metrics": {"lookback_weeks": "26"}}, "wrong_type", "metrics.lookback_weeks"),
        ({"metrics": {"lookback_weeks": True}}, "wrong_type", "metrics.lookback_weeks"),
        ({"metrics": {"lookback_weeks": 2.5}}, "wrong_type", "metrics.lookback_weeks"),
        ({"metrics": {"lookback_weeks": 0}}, "out_of_range", "metrics.lookback_weeks"),
        ({"metrics": {"reference_date": "31/01/2026"}}, "invalid_date", "metrics.reference_date"),
        ({"metrics": {"reference_date": 5}}, "wrong_type", "metrics.reference_date"),
        ({"metrics": {"duplicates": "drop"}}, "invalid_choice", "metrics.duplicates"),
        ({"metrics": {"ignored_texts": "x"}}, "wrong_type", "metrics.ignored_texts"),
        (
            {"metrics": {"count_deleted_as_activity": 1}},
            "wrong_type",
            "metrics.count_deleted_as_activity",
        ),
        (
            {"scoring": {"weights": {"frequency": -0.1}}},
            "out_of_range",
            "scoring.weights.frequency",
        ),
        ({"scoring": {"min_coverage": 0}}, "out_of_range", "scoring.min_coverage"),
        (
            {"scoring": {"thresholds": {"high": 0.3, "medium": 0.5}}},
            "threshold_order",
            "scoring.thresholds.high",
        ),
        ({"scoring": {"thresholds": {"medium": 0}}}, "threshold_order", "scoring.thresholds.high"),
        (
            {
                "scoring": {
                    "weights": {"frequency": 0, "reciprocity": 0, "initiative": 0, "recency": 0}
                }
            },
            "weights_zero",
            "scoring.weights",
        ),
        (
            {"metrics": {"lookback_weeks": 4}, "scoring": {"target_active_weeks": 8}},
            "target_exceeds_lookback",
            "scoring.target_active_weeks",
        ),
    ],
)
def test_invalid_settings_are_rejected(data: dict[str, Any], kind: str, key: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_settings(data)
    assert info.value.kind == kind
    assert info.value.details["key"] == key


def test_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as missing:
        load_settings(tmp_path / "absent.toml")
    assert missing.value.kind == "missing_file"
    broken = tmp_path / "broken.toml"
    broken.write_text("[metrics\n", encoding="utf-8")
    with pytest.raises(ConfigError) as syntax:
        load_settings(broken)
    assert syntax.value.kind == "syntax"


def test_integer_and_float_spellings_hash_the_same() -> None:
    as_int = parse_settings({"metrics": {"reply_window_hours": 48}})
    as_float = parse_settings({"metrics": {"reply_window_hours": 48.0}})
    reference = date(2026, 1, 31)
    assert config_hash(
        "metrics", METRICS_VERSION, as_int.metrics.resolved(reference)
    ) == config_hash("metrics", METRICS_VERSION, as_float.metrics.resolved(reference))


def test_metrics_hash_depends_on_reference_date_and_version() -> None:
    metrics = Settings().metrics
    first = config_hash("metrics", METRICS_VERSION, metrics.resolved(date(2026, 1, 31)))
    assert first != config_hash("metrics", METRICS_VERSION, metrics.resolved(date(2026, 2, 1)))
    assert first != config_hash("metrics", METRICS_VERSION + 1, metrics.resolved(date(2026, 1, 31)))


def test_ignored_texts_order_does_not_change_the_hash() -> None:
    a = parse_settings({"metrics": {"ignored_texts": ["x", "y"]}}).metrics
    b = parse_settings({"metrics": {"ignored_texts": ["y", "x", "x"]}}).metrics
    assert a.resolved(date(2026, 1, 1)) == b.resolved(date(2026, 1, 1))


def test_toml_datetime_is_not_a_reference_date() -> None:
    data = tomllib.loads("[metrics]\nreference_date = 2026-01-31T10:00:00\n")
    with pytest.raises(ConfigError) as info:
        parse_settings(data)
    assert info.value.kind == "wrong_type"


@pytest.mark.parametrize("value", ["0001-01-05", "9999-12-31", "1899-12-31"])
def test_reference_date_far_from_the_supported_range_is_rejected(value: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_settings({"metrics": {"reference_date": value}})
    assert info.value.kind == "out_of_range"


def test_ignored_texts_are_hashed_as_applied() -> None:
    a = parse_settings({"metrics": {"ignored_texts": ["ok"]}}).metrics
    b = parse_settings({"metrics": {"ignored_texts": [" ok "]}}).metrics
    assert a.resolved(date(2026, 1, 1)) == b.resolved(date(2026, 1, 1))
