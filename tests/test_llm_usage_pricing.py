"""Usage buckets, price tables and cost estimates."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from whatsapp_analyzer.config import ConfigError
from whatsapp_analyzer.llm.pricing import (
    builtin_prices,
    estimate_cost,
    load_prices,
    parse_price_table,
)
from whatsapp_analyzer.llm.tokens import estimate_prompt_tokens, estimate_text_tokens
from whatsapp_analyzer.llm.types import Usage


def table(**overrides: object) -> dict[str, Any]:
    entry: dict[str, object] = {
        "provider": "acme",
        "model": "m1",
        "currency": "USD",
        "unit": "per_million_tokens",
        "input": "3.00",
        "cache_read": "0.30",
        "cache_write": "3.75",
        "output": "15.00",
        "effective_from": date(2026, 1, 1),
        "source": "test fixture",
    }
    entry.update(overrides)
    return {"schema": 1, "prices": [{k: v for k, v in entry.items() if v is not None}]}


PRICE = parse_price_table(table(), "test").prices[0]


def test_usage_rejects_impossible_values() -> None:
    with pytest.raises(ValueError, match="negative"):
        Usage(input_uncached=-1)
    with pytest.raises(ValueError, match="part of output_total"):
        Usage(output_total=10, output_reasoning=11)


def test_unknown_counts_stay_unknown() -> None:
    usage = Usage(input_uncached=10, input_cache_read=None, input_cache_write=0)
    assert usage.input_total is None
    assert usage.to_dict()["input_cache_read"] is None


def test_cost_uses_disjoint_buckets_and_never_adds_reasoning_again() -> None:
    usage = Usage(
        input_uncached=1_000_000,
        input_cache_read=2_000_000,
        input_cache_write=100_000,
        output_total=500_000,
        output_reasoning=400_000,
    )
    cost = estimate_cost(usage, PRICE)
    # 3.00 + 2 * 0.30 + 0.1 * 3.75 + 0.5 * 15.00; reasoning is inside output_total.
    assert cost.total == Decimal("3.00") + Decimal("0.60") + Decimal("0.375") + Decimal("7.50")
    assert cost.missing == ()


def test_unknown_usage_gives_unknown_cost_not_zero() -> None:
    cost = estimate_cost(Usage(input_uncached=10, output_total=5), PRICE)
    assert cost.total is None
    assert set(cost.missing) == {"usage.input_cache_read", "usage.input_cache_write"}


def test_missing_rate_with_tokens_gives_unknown_cost() -> None:
    price = parse_price_table(table(cache_write=None), "test").prices[0]
    usage = Usage(input_uncached=1, input_cache_read=0, input_cache_write=5, output_total=1)
    cost = estimate_cost(usage, price)
    assert cost.total is None and cost.missing == ("price.cache_write",)
    # A missing rate does not matter when the bucket is zero.
    zero = Usage(input_uncached=1, input_cache_read=0, input_cache_write=0, output_total=1)
    assert estimate_cost(zero, price).total is not None


def test_no_price_means_unknown_cost() -> None:
    cost = estimate_cost(Usage(0, 0, 0, 0), None)
    assert cost.total is None and cost.missing == ("price",)


def test_costs_are_exact_decimals() -> None:
    usage = Usage(input_uncached=1, input_cache_read=0, input_cache_write=0, output_total=0)
    assert estimate_cost(usage, PRICE).total == Decimal("0.000003")


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (table(input=3.0), "wrong_type"),  # floats lose precision: strings are required
        (table(input="-1"), "out_of_range"),
        (table(input="abc"), "wrong_type"),
        (table(unit="per_token"), "invalid_choice"),
        (table(extra="x"), "unknown_key"),
        (table(source=None), "missing_key"),
        (table(effective_from="2026-01-01"), "wrong_type"),
        ({"schema": 2, "prices": []}, "out_of_range"),
    ],
)
def test_invalid_price_tables_are_rejected(data: dict[str, Any], kind: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_price_table(data, "test")
    assert info.value.kind == kind


def test_duplicate_entries_are_rejected() -> None:
    data = table()
    data["prices"].append(dict(data["prices"][0]))
    with pytest.raises(ConfigError) as info:
        parse_price_table(data, "test")
    assert info.value.kind == "duplicate_price"


def test_lookup_respects_effective_dates_and_workspace_override(tmp_path: Path) -> None:
    older = table()
    newer = table(effective_from=date(2026, 6, 1), input="2.00")
    prices = parse_price_table({"schema": 1, "prices": older["prices"] + newer["prices"]}, "test")
    assert prices.lookup("acme", "m1", date(2026, 3, 1)).input == Decimal("3.00")  # type: ignore[union-attr]
    assert prices.lookup("acme", "m1", date(2026, 7, 1)).input == Decimal("2.00")  # type: ignore[union-attr]
    assert prices.lookup("acme", "m1", date(2025, 1, 1)) is None
    assert prices.lookup("acme", "other", date(2026, 7, 1)) is None

    workspace_file = tmp_path / "pricing.toml"
    workspace_file.write_text(
        'schema = 1\n[[prices]]\nprovider = "fake"\nmodel = "fake-classifier"\n'
        'currency = "USD"\nunit = "per_million_tokens"\ninput = "1"\noutput = "2"\n'
        'effective_from = 2026-01-01\nsource = "local"\n',
        encoding="utf-8",
    )
    merged = load_prices(workspace_file)
    chosen = merged.lookup("fake", "fake-classifier", date(2026, 7, 1))
    assert chosen is not None and chosen.origin == "pricing.toml" and chosen.input == Decimal(1)


def test_builtin_table_has_no_real_provider_prices() -> None:
    assert {p.provider for p in builtin_prices().prices} == {"fake"}


def test_token_estimates_are_conservative_and_deterministic() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("abcd") == 2
    text = "Hello, could you send me the order number, please?"
    # Conservative: at least one token per 3 characters (real tokenizers give fewer).
    assert estimate_text_tokens(text) >= len(text) / 4
    assert estimate_prompt_tokens("sys", ["a", "b"]) == 8 + 1 + (1 + 4) * 2


def test_future_prices_are_not_in_effect_yet() -> None:
    prices = parse_price_table(table(effective_from=date(2099, 1, 1)), "test")
    assert prices.lookup("acme", "m1", date(2026, 6, 1)) is None


def test_datetimes_and_boolean_schema_are_rejected() -> None:
    with pytest.raises(ConfigError):
        parse_price_table(table(effective_from=datetime(2026, 1, 1)), "test")
    with pytest.raises(ConfigError):
        parse_price_table({"schema": True, "prices": []}, "test")
