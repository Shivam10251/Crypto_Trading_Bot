"""What a decision was made from, kept so the decision can be re-derived.

Raw market data has a finite life - quotes are purged after 7 days and books
after 3 - while opportunities are never purged. Without this module a stored
opportunity outlives the only evidence for it, and "why did the strategy
believe that?" becomes unanswerable a week later.

So the evidence travels *with* the opportunity rather than as a foreign key
into a table that will be emptied. Two reasons that is the right shape here:

- ``market_data`` holds a quote **sampled every 5 s**, not the quote the
  strategy priced against, so an id pointing at it would name a different
  observation and quietly look authoritative.
- Retention deletes those rows anyway; a dangling ``SET NULL`` reference
  preserves nothing.

Everything is plain data - no imports beyond the normalized vocabulary - so
the strategy layer stays free of the database that eventually stores it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_bot.db.models.enums import Side
from trading_bot.exchange.models import Fill, FundingInfo, MarketRef, MarketSpec, Quote


def _num(value: Decimal | None) -> str | None:
    """Decimals are stored as strings: JSON numbers are floats, and money is not."""
    return None if value is None else format(value, "f")


def _time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


@dataclass(frozen=True, slots=True)
class QuoteEvidence:
    """The top of book this leg was priced from."""

    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    local_timestamp: datetime
    exchange_timestamp: datetime | None
    sequence: int | None
    age_ms: int | None

    @classmethod
    def of(cls, quote: Quote, age_ms: int | None) -> QuoteEvidence:
        return cls(
            bid=quote.bid,
            ask=quote.ask,
            bid_size=quote.bid_size,
            ask_size=quote.ask_size,
            local_timestamp=quote.local_timestamp,
            exchange_timestamp=quote.exchange_timestamp,
            sequence=quote.sequence,
            age_ms=age_ms,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "bid": _num(self.bid),
            "ask": _num(self.ask),
            "bid_size": _num(self.bid_size),
            "ask_size": _num(self.ask_size),
            "local_timestamp": _time(self.local_timestamp),
            "exchange_timestamp": _time(self.exchange_timestamp),
            "sequence": self.sequence,
            "age_ms": self.age_ms,
        }


@dataclass(frozen=True, slots=True)
class FillEvidence:
    """One walk of a book: what was asked for, what filled, and at what levels."""

    side: Side
    requested: Decimal
    filled: Decimal
    average_price: Decimal | None
    levels: tuple[tuple[Decimal, Decimal], ...]

    @classmethod
    def of(cls, fill: Fill) -> FillEvidence:
        return cls(
            side=fill.side,
            requested=fill.requested,
            filled=fill.filled,
            average_price=fill.average_price,
            levels=tuple((level.price, level.size) for level in fill.levels),
        )

    @property
    def is_complete(self) -> bool:
        return self.filled >= self.requested

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side.value,
            "requested": _num(self.requested),
            "filled": _num(self.filled),
            "complete": self.is_complete,
            "average_price": _num(self.average_price),
            "levels": [[_num(price), _num(size)] for price, size in self.levels],
        }


@dataclass(frozen=True, slots=True)
class ConstraintEvidence:
    """The venue filters that decided the quantity, as they were read."""

    step_size: Decimal | None
    min_qty: Decimal | None
    max_qty: Decimal | None
    min_notional: Decimal | None

    @classmethod
    def of(cls, spec: MarketSpec | None) -> ConstraintEvidence:
        if spec is None:
            return cls(step_size=None, min_qty=None, max_qty=None, min_notional=None)
        return cls(
            step_size=spec.order_step_size,
            min_qty=spec.order_min_qty,
            max_qty=spec.order_max_qty,
            min_notional=spec.min_notional,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_size": _num(self.step_size),
            "min_qty": _num(self.min_qty),
            "max_qty": _num(self.max_qty),
            "min_notional": _num(self.min_notional),
        }


@dataclass(frozen=True, slots=True)
class LegEvidence:
    """One leg: its market, its quote, its book, and both walks of that book."""

    ref: MarketRef
    side: Side
    quote: QuoteEvidence
    book_sequence: int | None
    book_local_timestamp: datetime
    book_exchange_timestamp: datetime | None
    book_age_ms: int | None
    entry: FillEvidence
    # None when the opposite side of the book could not fill the unwind at all.
    unwind: FillEvidence | None
    constraints: ConstraintEvidence

    def as_dict(self) -> dict[str, Any]:
        return {
            "venue": self.ref.venue,
            "symbol": self.ref.symbol,
            "market_type": self.ref.market_type.value,
            "side": self.side.value,
            "quote": self.quote.as_dict(),
            "book": {
                "sequence": self.book_sequence,
                "local_timestamp": _time(self.book_local_timestamp),
                "exchange_timestamp": _time(self.book_exchange_timestamp),
                "age_ms": self.book_age_ms,
            },
            "entry_fill": self.entry.as_dict(),
            "unwind_fill": None if self.unwind is None else self.unwind.as_dict(),
            "constraints": self.constraints.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class MarketEvidence:
    """Everything the strategy saw, at the instant it decided."""

    evaluated_at: datetime
    requested_notional_usd: Decimal
    requested_quantity: Decimal
    executable_quantity: Decimal
    common_step_size: Decimal | None
    buy: LegEvidence
    sell: LegEvidence

    def as_dict(self) -> dict[str, Any]:
        return {
            "evaluated_at": _time(self.evaluated_at),
            "requested_notional_usd": _num(self.requested_notional_usd),
            "requested_quantity": _num(self.requested_quantity),
            "executable_quantity": _num(self.executable_quantity),
            "common_step_size": _num(self.common_step_size),
            "buy_leg": self.buy.as_dict(),
            "sell_leg": self.sell.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class FundingEvidence:
    """The funding observation used, and what was assumed about the future.

    ``rate_assumed_constant`` is the honest label on the weakest part of the
    estimate: the venue publishes the rate for the *next* settlement only, so
    charging more than one settlement reuses it for settlements nobody has
    announced yet.
    """

    rate: Decimal
    mark_price: Decimal
    index_price: Decimal
    next_funding_time: datetime
    interval_hours: int | None
    observed_at: datetime
    age_ms: int
    settlements: int
    rate_assumed_constant: bool

    @classmethod
    def of(cls, funding: FundingInfo, *, age_ms: int, settlements: int) -> FundingEvidence:
        return cls(
            rate=funding.last_funding_rate,
            mark_price=funding.mark_price,
            index_price=funding.index_price,
            next_funding_time=funding.next_funding_time,
            interval_hours=funding.funding_interval_hours,
            observed_at=funding.local_timestamp,
            age_ms=age_ms,
            settlements=settlements,
            rate_assumed_constant=settlements > 1,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "rate": _num(self.rate),
            "mark_price": _num(self.mark_price),
            "index_price": _num(self.index_price),
            "next_funding_time": _time(self.next_funding_time),
            "interval_hours": self.interval_hours,
            "observed_at": _time(self.observed_at),
            "age_ms": self.age_ms,
            "settlements": self.settlements,
            "rate_assumed_constant": self.rate_assumed_constant,
        }


@dataclass(frozen=True, slots=True)
class LegFeeEvidence:
    """The rate each leg was charged, and where that rate came from."""

    symbol: str
    market_type: str
    entry_role: str
    entry_rate_bps: Decimal
    exit_role: str
    exit_rate_bps: Decimal
    # True when the venue published the rate for this market and it overrode
    # the configured schedule.
    venue_reported: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market_type": self.market_type,
            "entry_role": self.entry_role,
            "entry_rate_bps": _num(self.entry_rate_bps),
            "exit_role": self.exit_role,
            "exit_rate_bps": _num(self.exit_rate_bps),
            "venue_reported": self.venue_reported,
        }


@dataclass(frozen=True, slots=True)
class PricingEvidence:
    """What the cost model did, and every assumption it did it under."""

    cost_model_version: str
    assumptions: Mapping[str, Any]
    fees: tuple[LegFeeEvidence, ...]
    funding: FundingEvidence | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "cost_model_version": self.cost_model_version,
            "assumptions": dict(self.assumptions),
            "fees": [fee.as_dict() for fee in self.fees],
            "funding": None if self.funding is None else self.funding.as_dict(),
        }
