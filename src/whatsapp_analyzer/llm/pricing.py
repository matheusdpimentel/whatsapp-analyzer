"""Versioned price tables and cost estimates.

Prices are per million tokens, written as strings ("3.00") so they are exact `Decimal`
values. Every entry records its effective date and source. The built-in table contains only
the offline fake provider; real prices are added in milestone 6 with their source and
retrieval date, and a workspace `pricing.toml` can always add or override entries.

A cost is never guessed: if a token bucket has tokens and no price, or a usage count is unknown,
the total is `None` and the missing pieces are listed.
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
_RATE_FIELDS = ("input", "cache_read", "cache_write", "output")
_REQUIRED = ("provider", "model", "currency", "unit", "input", "output", "effective_from", "source")
_OPTIONAL = ("cache_read", "cache_write", "retrieved_at")
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
    rates = {name: _rate(entry.get(name), f"{path}{name}") for name in _RATE_FIELDS}
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
    )


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


def estimate_cost(usage: Usage, price: Price | None) -> CostEstimate:
    if price is None:
        return CostEstimate(None, None, {}, ("price",), None)
    parts: dict[str, Decimal | None] = {}
    missing: list[str] = []
    for bucket, field in BUCKET_PRICES:
        tokens = getattr(usage, bucket)
        rate = getattr(price, field)
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
