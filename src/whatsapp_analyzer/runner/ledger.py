"""Budget accounting: consumption, reservations and the dispatch check.

Every attempt reserves its worst case before it is sent: the estimated input tokens plus the
maximum output tokens, and, with a price, that many tokens at the highest input or output
rate. The reservation is held while the attempt is in flight. When the attempt finishes:

- `billed` with complete usage: the reported (or, for the fake provider, estimated) counts
  and cost replace the reservation;
- `billed` with any count or cost unknown, or `unknown` billing: the reservation stays charged
  (`null` is never treated as zero);
- `not_sent` or `rejected`: nothing is charged.

Dispatching is allowed only when consumption plus all reservations plus the next worst case
fits every limit. Because the runner decides and reserves on a single thread before awaiting,
concurrent attempts can never overshoot together.

Token reservations rely on local estimates. They are conservative for the fake provider and
typical text, not mathematical guarantees for real providers.
"""

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from whatsapp_analyzer.llm.pricing import Price, estimate_cost
from whatsapp_analyzer.llm.types import Usage

_PER = Decimal(1_000_000)


@dataclass
class Reservation:
    tokens: int
    cost: Decimal | None


@dataclass
class Ledger:
    max_tokens: int | None
    max_cost: Decimal | None
    consumed_tokens: int = 0
    consumed_cost: Decimal = Decimal(0)
    inflight_tokens: int = 0
    inflight_cost: Decimal = Decimal(0)
    unknown_tokens: int = 0
    unknown_cost: Decimal = Decimal(0)
    # Set when any reservation or charge has no cost (no price, or an incomplete one): the
    # cost totals are then unknown and reported as null, never as a partial sum or zero.
    cost_unknown: bool = False
    # Budget cost added on top of the recorded costs of earlier attempts (see load_ledger).
    cost_adjustment: Decimal = Decimal(0)

    def note_adjustment(self, recorded: Decimal | None, budgeted: Decimal | None) -> None:
        if recorded is not None and budgeted is not None and budgeted > recorded:
            self.cost_adjustment += budgeted - recorded

    def fits(self, reservation: Reservation) -> str | None:
        """None when the next attempt fits, otherwise the limit that would be exceeded."""
        if self.max_tokens is not None:
            committed = self.consumed_tokens + self.inflight_tokens + self.unknown_tokens
            if committed + reservation.tokens > self.max_tokens:
                return "max_tokens"
        if self.max_cost is not None:
            assert reservation.cost is not None  # guaranteed by worst_case_cost checks
            committed_cost = self.consumed_cost + self.inflight_cost + self.unknown_cost
            if committed_cost + reservation.cost > self.max_cost:
                return "max_cost"
        return None

    def reserve(self, reservation: Reservation) -> None:
        self.inflight_tokens += reservation.tokens
        self.inflight_cost += self._cost(reservation.cost)

    def release(self, reservation: Reservation) -> None:
        self.inflight_tokens -= reservation.tokens
        if reservation.cost is not None:
            self.inflight_cost -= reservation.cost

    def charge(self, tokens: int, cost: Decimal | None) -> None:
        self.consumed_tokens += tokens
        self.consumed_cost += self._cost(cost)

    def hold_unknown(self, reservation: Reservation) -> None:
        self.unknown_tokens += reservation.tokens
        self.unknown_cost += self._cost(reservation.cost)

    def _cost(self, cost: Decimal | None) -> Decimal:
        if cost is None:
            self.cost_unknown = True
            return Decimal(0)  # excluded from totals: they are reported as unknown
        return cost

    def costs(self) -> dict[str, str | None]:
        def shown(value: Decimal) -> str | None:
            return None if self.cost_unknown else str(value)

        return {
            "consumed": shown(self.consumed_cost),
            "in_flight": shown(self.inflight_cost),
            "unknown_billing": shown(self.unknown_cost),
            "committed": shown(self.consumed_cost + self.inflight_cost + self.unknown_cost),
            "adjustment": str(self.cost_adjustment),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "max_tokens": self.max_tokens,
            "max_cost": None if self.max_cost is None else str(self.max_cost),
            "consumed_tokens": self.consumed_tokens,
            "inflight_tokens": self.inflight_tokens,
            "unknown_reserved_tokens": self.unknown_tokens,
            "costs": self.costs(),
        }


def worst_case_rate(price: Price | None) -> Decimal | None:
    """Highest per-token rate of a complete price entry, across every tier; None when any rate
    is missing, or when the entry has a tier ceiling without long rates (a larger request
    could then not be priced, so no reservation could be trusted)."""
    if price is None or price.cache_read is None or price.cache_write is None:
        return None
    rates = [price.input, price.output, price.cache_read, price.cache_write]
    if price.max_input_tokens is not None:
        if not price.has_long_rates:
            return None
        rates.extend(r for r in price.rates(long=True).values() if r is not None)
    return max(rates) / _PER


def reservation_for(estimated_input: int, max_output: int, rate: Decimal | None) -> Reservation:
    tokens = estimated_input + max_output
    return Reservation(tokens, None if rate is None else rate * tokens)


def load_ledger(
    connection: sqlite3.Connection,
    run_id: int,
    max_tokens: int | None,
    max_cost: Decimal | None,
    price: Price | None = None,
) -> Ledger:
    """Rebuild the ledger of a run from its attempts (used when a run starts or resumes).

    With `price` (the run's own price snapshot), every cost that counts toward the budget is
    at least what that snapshot gives for the attempt's stored usage, or for its stored
    reserved tokens. A snapshot can gain tier information when a run resumes
    (`service.with_known_tier`), so attempts recorded before at a too-low rate are charged at
    the applicable one. The stored costs are never changed: they stay the historical record,
    and the difference is reported as `cost_adjustment`. Nothing is persisted, so resuming
    any number of times applies the correction once, and the run's snapshot (not the current
    price table) is used, so a later price change never re-prices a run."""
    ledger = Ledger(max_tokens, max_cost)
    rate = worst_case_rate(price)
    for row in connection.execute(
        "SELECT a.* FROM llm_attempts a JOIN llm_jobs j ON j.id = a.job_id WHERE j.run_id = ?",
        (run_id,),
    ):
        recorded = None if row["reserved_cost"] is None else Decimal(row["reserved_cost"])
        budgeted = _at_least(recorded, None if rate is None else rate * row["reserved_tokens"])
        reservation = Reservation(row["reserved_tokens"], budgeted)
        billing = row["billing"]
        if billing == "pending":  # dispatched and not finished: in flight right now
            ledger.reserve(reservation)
            ledger.note_adjustment(recorded, budgeted)
        elif billing == "unknown":
            ledger.hold_unknown(reservation)
            ledger.note_adjustment(recorded, budgeted)
        elif billing == "billed":
            tokens, cost = charged(row, reservation, price)
            ledger.charge(tokens, cost)
            stored = None if row["cost"] is None else Decimal(row["cost"])
            ledger.note_adjustment(stored if stored is not None else recorded, cost)
    return ledger


def charged(
    row: sqlite3.Row, reservation: Reservation, price: Price | None = None
) -> tuple[int, Decimal | None]:
    """Tokens and cost charged for a billed attempt: actual values when all are known,
    otherwise the reservation. With `price`, the cost is at least the cost of the stored usage
    at that price (never lower than what was recorded)."""
    parts = [row[name] for name in ("input_uncached", "input_cache_read", "input_cache_write")]
    output = row["output_total"]
    complete = output is not None and all(p is not None for p in parts)
    tokens = sum(parts) + output if complete else reservation.tokens
    recorded = None if row["cost"] is None else Decimal(row["cost"])
    recomputed = None
    if price is not None and complete:
        usage = Usage(
            input_uncached=parts[0],
            input_cache_read=parts[1],
            input_cache_write=parts[2],
            output_total=output,
        )
        recomputed = estimate_cost(usage, price).total
    cost = _at_least(recorded, recomputed)
    if cost is not None:
        return tokens, cost
    return tokens, reservation.cost  # None when there is no usable price


def _at_least(recorded: Decimal | None, recomputed: Decimal | None) -> Decimal | None:
    """The larger of two costs; one that is unknown never lowers the other."""
    if recorded is None:
        return recomputed
    if recomputed is None:
        return recorded
    return max(recorded, recomputed)
