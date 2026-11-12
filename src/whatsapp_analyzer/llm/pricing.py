"""Versioned price tables and cost estimates.

Prices are per million tokens, written as strings ("3.00") so they are exact `Decimal`
values. Every entry records its effective date and source, and real entries the date they
were retrieved (`retrieved_at`); an entry retrieved more than `STALE_AFTER_DAYS` ago is
reported as stale. A workspace `pricing.toml` can always add or override entries. A cost is
an estimate from these prices, never an exact bill.

A cost is never guessed: if a token bucket has tokens and no price, or a usage count is unknown,
the total is `None` and the missing pieces are listed.

Tiered prices: an entry may cover only requests with at most `max_input_tokens` input tokens
(all input buckets, cached and written ones included). Above it, the `long_*` rates apply to
the WHOLE request. The tier is chosen from the reported input total; when the input total is
unknown, the higher (long) rates are used, never the cheaper ones. An entry with a ceiling and
no long rates cannot price larger requests: their cost is unknown, and such an entry cannot
back a hard cost limit.
"""

import tomllib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Any

from whatsapp_analyzer.config import ConfigError
from whatsapp_analyzer.llm.types import Usage

PRICE_SCHEMA = 1
PRICE_UNIT = "per_million_tokens"
_PER = Decimal(1_000_000)
STALE_AFTER_DAYS = 90
_RATE_FIELDS = ("input", "cache_read", "cache_write", "output")
LONG_RATE_FIELDS = ("long_input", "long_cache_read", "long_cache_write", "long_output")
_LONG_FIELDS = LONG_RATE_FIELDS
_REQUIRED = ("provider", "model", "currency", "unit", "input", "output", "effective_from", "source")
_OPTIONAL = ("cache_read", "cache_write", "retrieved_at", "max_input_tokens", *_LONG_FIELDS)
# usage bucket -> price field
BUCKET_PRICES = (
    ("input_uncached", "input"),
    ("input_cache_read", "cache_read"),
    ("input_cache_write", "cache_write"),
    ("output_total", "output"),
)


@dataclass(frozen=True)
class Price:
    provider: str
    model: str
    currency: str
    input: Decimal
    output: Decimal
    cache_read: Decimal | None
    cache_write: Decimal | None
    effective_from: date
    source: str
    origin: str  # "built-in" or the workspace file name
    retrieved_at: date | None = None
    # Tiered pricing: the short-context rates above cover requests with at most this many
    # input tokens; larger requests are billed entirely at the long rates (when declared).
    max_input_tokens: int | None = None
    long_input: Decimal | None = None
    long_cache_read: Decimal | None = None
    long_cache_write: Decimal | None = None
    long_output: Decimal | None = None

    @property
    def has_long_rates(self) -> bool:
        return self.long_input is not None

    def rates(self, long: bool) -> dict[str, Decimal | None]:
        """Rate per price field ("input", "cache_read", ...) for one tier."""
        if long:
            return {
                "input": self.long_input,
                "cache_read": self.long_cache_read,
                "cache_write": self.long_cache_write,
                "output": self.long_output,
            }
        return {
            "input": self.input,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "output": self.output,
        }

    def stale(self, today: date) -> bool:
        """True when the entry may be outdated: retrieved long ago, or never dated."""
        if self.provider == "fake":
            return False
        reference = self.retrieved_at or self.effective_from
        return (today - reference).days > STALE_AFTER_DAYS


@dataclass(frozen=True)
class CostEstimate:
    currency: str | None
    total: Decimal | None
    parts: dict[str, Decimal | None]
    missing: tuple[str, ...]
    price_effective_from: date | None

    def to_dict(self) -> dict[str, object]:
        return {
            "currency": self.currency,
            "total": None if self.total is None else str(self.total),
            "parts": {k: None if v is None else str(v) for k, v in self.parts.items()},
            "missing": list(self.missing),
            "price_effective_from": (
                self.price_effective_from.isoformat() if self.price_effective_from else None
            ),
        }


class PriceTable:
    def __init__(self, prices: list[Price]) -> None:
        self.prices = prices

    def lookup(self, provider: str, model: str, on: date) -> Price | None:
        """The entry in effect on `on` (entries dated later are not in effect yet). Workspace
        entries win over built-in ones with the same effective date."""
        candidates = [
            p
            for p in self.prices
            if p.provider == provider and p.model == model and p.effective_from <= on
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: (p.effective_from, p.origin != "built-in"))

    def merged(self, other: "PriceTable") -> "PriceTable":
        return PriceTable([*self.prices, *other.prices])


def builtin_prices() -> PriceTable:
    text = resources.files(__package__).joinpath("prices.toml").read_text(encoding="utf-8")
    return parse_price_table(tomllib.loads(text), "built-in")


def load_prices(workspace_file: Path | None) -> PriceTable:
    table = builtin_prices()
    if workspace_file is None or not workspace_file.is_file():
        return table
    try:
        data = tomllib.loads(workspace_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError("unreadable", path=str(workspace_file)) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("syntax", path=str(workspace_file), reason=str(exc)) from exc
    return table.merged(parse_price_table(data, workspace_file.name))


def parse_price_table(data: dict[str, Any], origin: str) -> PriceTable:
    unknown = sorted(set(data) - {"schema", "prices"})
    if unknown:
        raise ConfigError("unknown_key", key=unknown[0])
    if type(data.get("schema")) is not int or data["schema"] != PRICE_SCHEMA:
        raise ConfigError("out_of_range", key="schema", minimum=PRICE_SCHEMA, maximum=PRICE_SCHEMA)
    entries = data.get("prices", [])
    if not isinstance(entries, list):
        raise ConfigError("wrong_type", key="prices", expected="array of tables")
    prices = [_price(entry, index, origin) for index, entry in enumerate(entries)]
    keys = [(p.provider, p.model, p.effective_from) for p in prices]
    if len(keys) != len(set(keys)):
        raise ConfigError("duplicate_price", key="prices")
    return PriceTable(prices)


def _price(entry: object, index: int, origin: str) -> Price:
    path = f"prices[{index}]."
    if not isinstance(entry, dict):
        raise ConfigError("wrong_type", key=path.rstrip("."), expected="table")
    unknown = sorted(set(entry) - set(_REQUIRED) - set(_OPTIONAL))
    if unknown:
        raise ConfigError("unknown_key", key=f"{path}{unknown[0]}")
    for name in _REQUIRED:
        if name not in entry:
            raise ConfigError("missing_key", key=f"{path}{name}")
    for name in ("provider", "model", "currency", "source"):
        if not isinstance(entry[name], str) or not entry[name].strip():
            raise ConfigError("wrong_type", key=f"{path}{name}", expected="string")
    if entry["unit"] != PRICE_UNIT:
        raise ConfigError("invalid_choice", key=f"{path}unit", choices=PRICE_UNIT)
    effective = entry["effective_from"]
    if type(effective) is not date:  # a TOML datetime is a date subclass: reject it
        raise ConfigError("wrong_type", key=f"{path}effective_from", expected="date")
    retrieved = entry.get("retrieved_at")
    if retrieved is not None and type(retrieved) is not date:
        raise ConfigError("wrong_type", key=f"{path}retrieved_at", expected="date")
    ceiling = entry.get("max_input_tokens")
    if ceiling is not None and (isinstance(ceiling, bool) or not isinstance(ceiling, int)):
        raise ConfigError("wrong_type", key=f"{path}max_input_tokens", expected="integer")
    if ceiling is not None and ceiling < 1:
        raise ConfigError("out_of_range", key=f"{path}max_input_tokens", minimum=1, maximum="any")
    rates = {name: _rate(entry.get(name), f"{path}{name}") for name in _RATE_FIELDS}
    long_rates = _long_rates(entry, path, ceiling)
    input_rate, output_rate = rates["input"], rates["output"]
    assert input_rate is not None and output_rate is not None  # required above
    return Price(
        provider=entry["provider"],
        model=entry["model"],
        currency=entry["currency"],
        input=input_rate,
        output=output_rate,
        cache_read=rates["cache_read"],
        cache_write=rates["cache_write"],
        effective_from=effective,
        source=entry["source"],
        origin=origin,
        retrieved_at=retrieved,
        max_input_tokens=ceiling,
        long_input=long_rates["long_input"],
        long_cache_read=long_rates["long_cache_read"],
        long_cache_write=long_rates["long_cache_write"],
        long_output=long_rates["long_output"],
    )


def _long_rates(entry: dict[str, Any], path: str, ceiling: int | None) -> dict[str, Decimal | None]:
    """All four long-context rates or none, and only together with a ceiling."""
    rates = {name: _rate(entry.get(name), f"{path}{name}") for name in _LONG_FIELDS}
    declared = [name for name, value in rates.items() if value is not None]
    if declared and len(declared) != len(_LONG_FIELDS):
        missing = next(name for name in _LONG_FIELDS if rates[name] is None)
        raise ConfigError("missing_key", key=f"{path}{missing}")
    if declared and ceiling is None:
        raise ConfigError("missing_key", key=f"{path}max_input_tokens")
    return rates


def _rate(value: object, key: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):  # floats would silently lose precision
        raise ConfigError("wrong_type", key=key, expected='decimal string, e.g. "3.00"')
    try:
        rate = Decimal(value)
    except InvalidOperation as exc:
        raise ConfigError("wrong_type", key=key, expected="decimal string") from exc
    if not rate.is_finite() or rate < 0:
        raise ConfigError("out_of_range", key=key, minimum=0, maximum="any")
    return rate


def long_tier(usage: Usage, price: Price) -> bool | None:
    """True when the long rates apply, False for the short ones, None when the entry cannot
    price this request (above the ceiling without long rates)."""
    if price.max_input_tokens is None:
        return False
    total = usage.input_total
    if total is not None and total <= price.max_input_tokens:
        return False
    # above the ceiling, or unknown input: never assume the cheaper tier
    return True if price.has_long_rates else None


def estimate_cost(usage: Usage, price: Price | None) -> CostEstimate:
    if price is None:
        return CostEstimate(None, None, {}, ("price",), None)
    tier = long_tier(usage, price)
    if tier is None:
        return CostEstimate(price.currency, None, {}, ("price.long_context",), price.effective_from)
    rates = price.rates(tier)
    parts: dict[str, Decimal | None] = {}
    missing: list[str] = []
    for bucket, field in BUCKET_PRICES:
        tokens = getattr(usage, bucket)
        rate = rates[field]
        if tokens is None:
            missing.append(f"usage.{bucket}")
            parts[bucket] = None
        elif tokens == 0:
            parts[bucket] = Decimal(0)
        elif rate is None:
            missing.append(f"price.{field}")
            parts[bucket] = None
        else:
            parts[bucket] = Decimal(tokens) * rate / _PER
    known = [v for v in parts.values() if v is not None]
    total = sum(known, Decimal(0)) if not missing else None
    return CostEstimate(price.currency, total, parts, tuple(missing), price.effective_from)
