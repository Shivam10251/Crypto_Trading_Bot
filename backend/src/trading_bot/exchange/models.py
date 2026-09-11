"""Normalized market-data types.

This is the vocabulary the rest of the platform speaks. Strategies, the cost
model and the risk engine consume these objects and never touch a venue payload,
so adding a second exchange means writing one adapter, not editing strategy code.

Design notes:
- ``Decimal`` everywhere for prices and sizes; float drift is unacceptable.
- Frozen slotted dataclasses: these are allocated on every tick, so the memory
  and attribute-access cost matters.
- Invariants are enforced in ``__post_init__``. A malformed quote cannot be
  constructed, which means it can never reach a strategy.
- ``exchange_timestamp`` is optional because not every venue reports one:
  Binance spot bookTicker and depth carry no event time, while USD-M futures
  do. Substituting local time there would fabricate a latency measurement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Self

from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.errors import ExchangeDataError

BPS_SCALE = Decimal(10_000)


def _latency_ms(local: datetime, exchange: datetime | None) -> int | None:
    """Exchange-to-local delay, or ``None`` when the venue sent no clock."""
    if exchange is None:
        return None
    return int((local - exchange).total_seconds() * 1000)


@dataclass(frozen=True, slots=True)
class MarketRef:
    """Identity of an instrument: enough to route a request or key a cache."""

    venue: str
    symbol: str
    market_type: MarketType

    def __str__(self) -> str:
        return f"{self.venue}:{self.symbol}:{self.market_type.value}"


@dataclass(frozen=True, slots=True)
class MarketSpec:
    """Instrument reference data, including the venue's trading filters.

    Fees are ``None`` when the venue only exposes them behind an authenticated
    endpoint (Binance spot). The cost model falls back to configuration rather
    than guessing - see Phase 6.
    """

    ref: MarketRef
    base_asset: str
    quote_asset: str
    is_active: bool
    tick_size: Decimal | None = None
    step_size: Decimal | None = None
    min_notional: Decimal | None = None
    maker_fee_bps: Decimal | None = None
    taker_fee_bps: Decimal | None = None
    contract_size: Decimal | None = None
    settlement_asset: str | None = None

    @property
    def symbol(self) -> str:
        return self.ref.symbol


@dataclass(frozen=True, slots=True)
class Quote:
    """Top-of-book snapshot: the atom of market data."""

    ref: MarketRef
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    local_timestamp: datetime
    exchange_timestamp: datetime | None = None
    # Venue update id, used to detect gaps and duplicate messages.
    sequence: int | None = None

    def __post_init__(self) -> None:
        if self.bid <= 0 or self.ask <= 0:
            raise ExchangeDataError(f"non-positive price for {self.ref}: {self.bid}/{self.ask}")
        if self.bid_size < 0 or self.ask_size < 0:
            raise ExchangeDataError(f"negative size for {self.ref}")
        if self.ask < self.bid:
            # A crossed top-of-book on one venue means bad data, not free money.
            raise ExchangeDataError(f"crossed book for {self.ref}: bid {self.bid} > ask {self.ask}")
        if self.local_timestamp.tzinfo is None:
            raise ExchangeDataError("local_timestamp must be timezone-aware")

    @property
    def mid_price(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> Decimal:
        return (self.spread / self.mid_price) * BPS_SCALE

    @property
    def latency_ms(self) -> int | None:
        """Exchange-to-local delay, or ``None`` when the venue sends no clock."""
        if self.exchange_timestamp is None:
            return None
        delta = (self.local_timestamp - self.exchange_timestamp).total_seconds()
        return int(delta * 1000)

    def age_ms(self, now: datetime | None = None) -> int:
        """How old this quote is against our own clock.

        Used for staleness checks. Deliberately based on ``local_timestamp``:
        it is the one clock that is always present and never venue-controlled.
        """
        moment = now or datetime.now(UTC)
        return int((moment - self.local_timestamp).total_seconds() * 1000)


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    size: Decimal

    @property
    def notional(self) -> Decimal:
        return self.price * self.size


@dataclass(frozen=True, slots=True)
class OrderBook:
    """Depth snapshot, best price first on both sides."""

    ref: MarketRef
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    local_timestamp: datetime
    exchange_timestamp: datetime | None = None
    sequence: int | None = None

    def __post_init__(self) -> None:
        if not self.bids or not self.asks:
            raise ExchangeDataError(f"empty book side for {self.ref}")
        if self.best_bid > self.best_ask:
            raise ExchangeDataError(f"crossed book for {self.ref}")
        # Ordering matters: fills walk these lists in order.
        if any(a.price < b.price for a, b in zip(self.bids, self.bids[1:], strict=False)):
            raise ExchangeDataError(f"bids not descending for {self.ref}")
        if any(a.price > b.price for a, b in zip(self.asks, self.asks[1:], strict=False)):
            raise ExchangeDataError(f"asks not ascending for {self.ref}")

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0].price

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0].price

    @property
    def mid_price(self) -> Decimal:
        return (self.best_bid + self.best_ask) / 2

    def depth_notional(self, side: Side, levels: int | None = None) -> Decimal:
        """Total value resting on one side - the liquidity actually available."""
        book = self.bids if side is Side.BUY else self.asks
        selected = book if levels is None else book[:levels]
        return sum((level.notional for level in selected), Decimal(0))

    def imbalance(self, levels: int = 5) -> Decimal:
        """Bid pressure minus ask pressure, in [-1, 1].

        Positive means more size on the bid. Phase 4 uses this as a monitoring
        signal; it is computed here because it is a property of the book.
        """
        bid_side = self.depth_notional(Side.BUY, levels)
        ask_side = self.depth_notional(Side.SELL, levels)
        total = bid_side + ask_side
        if total == 0:
            return Decimal(0)
        return (bid_side - ask_side) / total

    def fill_price(self, side: Side, quantity: Decimal) -> tuple[Decimal, Decimal]:
        """Walk the book for ``quantity``; return (average price, filled size).

        This is how slippage stops being a guess: a market buy consumes asks
        from the top down. Returns the partial fill when depth runs out, so
        callers can see they cannot get the size they wanted.
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        book = self.asks if side is Side.BUY else self.bids
        remaining = quantity
        cost = Decimal(0)
        for level in book:
            if remaining <= 0:
                break
            take = min(remaining, level.size)
            cost += take * level.price
            remaining -= take
        filled = quantity - remaining
        if filled == 0:
            return Decimal(0), Decimal(0)
        return cost / filled, filled


@dataclass(frozen=True, slots=True)
class DepthDiff:
    """One incremental order-book update from a venue's depth stream.

    Sizes are absolute, not deltas: a level's size replaces what the local book
    holds, and size zero deletes the level. That is what makes re-applying an
    update the snapshot already reflects harmless.

    The update ids let a consumer prove it missed nothing: this message covers
    ``first_update_id..final_update_id``, and ``previous_final_update_id`` -
    when the venue sends it - names the message that must have come before.
    """

    ref: MarketRef
    first_update_id: int
    final_update_id: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    local_timestamp: datetime
    exchange_timestamp: datetime | None = None
    previous_final_update_id: int | None = None

    def __post_init__(self) -> None:
        if self.first_update_id > self.final_update_id:
            raise ExchangeDataError(
                f"depth update ids reversed for {self.ref}: "
                f"{self.first_update_id} > {self.final_update_id}"
            )
        for level in (*self.bids, *self.asks):
            if level.price <= 0 or level.size < 0:
                raise ExchangeDataError(f"invalid depth level for {self.ref}: {level}")
        if self.local_timestamp.tzinfo is None:
            raise ExchangeDataError("local_timestamp must be timezone-aware")

    @property
    def latency_ms(self) -> int | None:
        return _latency_ms(self.local_timestamp, self.exchange_timestamp)


@dataclass(frozen=True, slots=True)
class TickerStats:
    """Rolling 24-hour statistics - where traded volume comes from."""

    ref: MarketRef
    last_price: Decimal
    # Base-asset and quote-asset volume over the rolling window.
    volume: Decimal
    quote_volume: Decimal
    exchange_timestamp: datetime
    local_timestamp: datetime

    def __post_init__(self) -> None:
        if self.last_price <= 0:
            raise ExchangeDataError(f"non-positive last price for {self.ref}")
        if self.volume < 0 or self.quote_volume < 0:
            raise ExchangeDataError(f"negative volume for {self.ref}")
        if self.local_timestamp.tzinfo is None:
            raise ExchangeDataError("local_timestamp must be timezone-aware")

    @property
    def latency_ms(self) -> int | None:
        return _latency_ms(self.local_timestamp, self.exchange_timestamp)


@dataclass(frozen=True, slots=True)
class TradePrint:
    """A public trade, used to calibrate slippage against reality."""

    ref: MarketRef
    trade_id: str
    price: Decimal
    quantity: Decimal
    exchange_timestamp: datetime
    local_timestamp: datetime
    aggressor_side: Side | None = None

    def __post_init__(self) -> None:
        if self.price <= 0 or self.quantity <= 0:
            raise ExchangeDataError(f"invalid trade print for {self.ref}")


@dataclass(frozen=True, slots=True)
class FundingInfo:
    """Perpetual funding state.

    Funding is a real cost of holding a perpetual leg, so the spot/perp strategy
    cannot be evaluated without it.
    """

    ref: MarketRef
    mark_price: Decimal
    index_price: Decimal
    last_funding_rate: Decimal
    next_funding_time: datetime
    local_timestamp: datetime

    @property
    def last_funding_rate_bps(self) -> Decimal:
        return self.last_funding_rate * BPS_SCALE


@dataclass(frozen=True, slots=True)
class Balance:
    """Account balance for one asset. Requires credentials; unused in Phase 2."""

    asset: str
    free: Decimal
    locked: Decimal = Decimal(0)

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass(frozen=True, slots=True)
class ServerTime:
    """Venue clock alongside ours, for measuring skew."""

    exchange_time: datetime
    local_time: datetime
    round_trip_ms: int

    @property
    def skew_ms(self) -> int:
        """Venue clock minus ours, corrected for half the round trip."""
        raw = (self.exchange_time - self.local_time).total_seconds() * 1000
        return int(raw + self.round_trip_ms / 2)


@dataclass(frozen=True, slots=True)
class MarketDataSubscription:
    """What a caller wants streamed; a venue's stream source turns it into connections."""

    refs: tuple[MarketRef, ...]
    include_depth: bool = False
    depth_levels: int = 10
    include_trades: bool = False
    # Rolling 24h statistics, which is where traded volume comes from.
    include_ticker: bool = False
    _created_at: datetime = field(default_factory=lambda: datetime.now(UTC), repr=False)

    @classmethod
    def top_of_book(cls, *refs: MarketRef) -> Self:
        return cls(refs=tuple(refs))
