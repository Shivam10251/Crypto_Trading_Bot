"""Market reference data and raw market observations.

These tables are the head of the traceability chain: every opportunity points
back to the exact ``market_data`` row that produced it, so any decision can be
re-derived from the quotes that caused it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trading_bot.db.base import Base, RecordMixin
from trading_bot.db.models.enum_types import MARKET_TYPE, SIDE
from trading_bot.db.models.enums import MarketType, Side
from trading_bot.db.models.types import BPS, MONEY, PRICE, QUANTITY, SYMBOL_LENGTH


class Market(Base, RecordMixin):
    """An instrument on a venue: ``BTCUSDT`` spot and ``BTCUSDT`` perp are two rows.

    Exchange filters (tick size, step size, minimum notional) live here because
    execution must respect them; an order violating a filter is rejected by the
    venue, which the paper simulator reproduces.
    """

    __tablename__ = "markets"

    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(SYMBOL_LENGTH), nullable=False)
    market_type: Mapped[MarketType] = mapped_column(MARKET_TYPE, nullable=False)
    base_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(16), nullable=False)

    # Exchange trading filters. LOT_SIZE gives step/min/max quantity and
    # MARKET_LOT_SIZE a separate, usually tighter cap on market orders - on
    # every USD-M perpetual measured, its maxQty is the binding one. Both are
    # stored because an order has to satisfy both.
    tick_size: Mapped[Decimal | None] = mapped_column(PRICE)
    step_size: Mapped[Decimal | None] = mapped_column(QUANTITY)
    min_notional: Mapped[Decimal | None] = mapped_column(MONEY)
    min_qty: Mapped[Decimal | None] = mapped_column(QUANTITY)
    max_qty: Mapped[Decimal | None] = mapped_column(QUANTITY)
    market_min_qty: Mapped[Decimal | None] = mapped_column(QUANTITY)
    market_max_qty: Mapped[Decimal | None] = mapped_column(QUANTITY)
    market_step_size: Mapped[Decimal | None] = mapped_column(QUANTITY)

    # Venue fees for this market, in basis points.
    maker_fee_bps: Mapped[Decimal | None] = mapped_column(BPS)
    taker_fee_bps: Mapped[Decimal | None] = mapped_column(BPS)

    # Perpetual/future contracts only.
    contract_size: Mapped[Decimal | None] = mapped_column(QUANTITY)
    settlement_asset: Mapped[str | None] = mapped_column(String(16))

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Chosen by the market-data service's latest start, and cleared for every
    # other market then - so the API knows which markets should be live
    # without re-running the selection itself.
    is_monitored: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    __table_args__ = (
        # One row per instrument per venue.
        UniqueConstraint("venue", "symbol", "market_type", name="venue_symbol_type"),
        Index("ix_markets_active", "is_active", "market_type"),
        CheckConstraint("tick_size IS NULL OR tick_size > 0", name="tick_size_positive"),
        CheckConstraint("step_size IS NULL OR step_size > 0", name="step_size_positive"),
        CheckConstraint(
            "market_step_size IS NULL OR market_step_size > 0", name="market_step_size_positive"
        ),
        CheckConstraint("min_qty IS NULL OR min_qty > 0", name="min_qty_positive"),
        CheckConstraint("max_qty IS NULL OR max_qty > 0", name="max_qty_positive"),
        CheckConstraint(
            "market_min_qty IS NULL OR market_min_qty > 0", name="market_min_qty_positive"
        ),
        CheckConstraint(
            "market_max_qty IS NULL OR market_max_qty > 0", name="market_max_qty_positive"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Market {self.venue}:{self.symbol}:{self.market_type}>"


class MarketData(Base, RecordMixin):
    """Normalized top-of-book snapshot.

    Both the exchange timestamp and the local receipt timestamp are stored so
    latency is measured rather than assumed, and staleness can be judged against
    the clock that matters.
    """

    __tablename__ = "market_data"

    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )

    bid: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    ask: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    bid_size: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    ask_size: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)

    # Derived on write so queries and the dashboard never recompute them.
    mid_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    spread: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    spread_bps: Mapped[Decimal] = mapped_column(BPS, nullable=False)

    volume_24h: Mapped[Decimal | None] = mapped_column(QUANTITY)

    # NULL when the venue does not report its own clock: Binance spot
    # bookTicker and depth carry no event time, while USD-M futures do. Storing
    # the local time here instead would fabricate a measurement.
    exchange_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # exchange -> local delta; NULL whenever exchange_timestamp is.
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # Venue update id, used to detect gaps and duplicate messages.
    sequence: Mapped[int | None] = mapped_column(BigInteger)

    market: Mapped[Market] = relationship(lazy="raise")

    __table_args__ = (
        # Primary access pattern: latest quotes for a market.
        Index("ix_market_data_market_exch_ts", "market_id", "exchange_timestamp"),
        # Retention purges scan on local_timestamp.
        Index("ix_market_data_local_ts", "local_timestamp"),
        # Invalid data is rejected by the database, not merely logged.
        CheckConstraint("bid > 0 AND ask > 0", name="prices_positive"),
        CheckConstraint("bid_size >= 0 AND ask_size >= 0", name="sizes_non_negative"),
        # A crossed top-of-book on a single venue means bad data. Refusing it
        # here keeps the research dataset trustworthy.
        CheckConstraint("ask >= bid", name="book_not_crossed"),
        # Latency is exchange -> local; it cannot exist without both clocks.
        CheckConstraint(
            "latency_ms IS NULL OR exchange_timestamp IS NOT NULL",
            name="latency_requires_exchange_clock",
        ),
    )


class OrderBookSnapshot(Base, RecordMixin):
    """Sampled order-book depth.

    Depth is sampled rather than streamed to storage: full continuous depth for
    50+ markets would dominate the database while adding little research value
    beyond the levels that affect fills.

    Written only from a book the engine had SYNCED (Phase 11's capture), so a
    row is depth the engine vouched for at ``local_timestamp``. Sampling has a
    cost the replay reports rather than hides: between two rows the book is
    unknown, so a replayed fill can only see the latest sampled book at its
    arrival time - never the book the venue actually held then.
    """

    __tablename__ = "order_books"

    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )
    # [[price, size], ...] best-first. JSONB keeps one row per snapshot instead
    # of exploding into millions of level rows.
    bids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    asks: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    depth_levels: Mapped[int] = mapped_column(Integer, nullable=False)
    # Whether each side is ALL the depth the venue had. Always false for a
    # capped Binance snapshot: exhausting it means "more is unknown", not "no
    # more liquidity", and a replayed walk past it is DEPTH_TRUNCATED.
    bids_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    asks_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    # NULL for venues that omit an event time (Binance spot depth).
    exchange_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sequence: Mapped[int | None] = mapped_column(BigInteger)

    market: Mapped[Market] = relationship(lazy="raise")

    __table_args__ = (
        Index("ix_order_books_market_exch_ts", "market_id", "exchange_timestamp"),
        Index("ix_order_books_local_ts", "local_timestamp"),
        CheckConstraint("depth_levels > 0", name="depth_positive"),
    )


class FundingObservation(Base, RecordMixin):
    """One poll of a perpetual's funding state, as the venue reported it.

    ``funding_rate`` is the venue's rate for the settlement at
    ``next_funding_time`` *as known at* ``local_timestamp``: until that
    settlement it is a moving estimate, so only an observation taken shortly
    before a settlement says what that settlement actually charged. That is
    the rule replay uses to attribute funding, and why an observation long
    before a settlement leaves funding unmeasured rather than guessed.
    """

    __tablename__ = "funding_observations"

    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )
    mark_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    index_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    funding_rate: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    next_funding_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # NULL when the venue does not publish the interval; replay then cannot
    # place settlements and funding stays unmeasured.
    funding_interval_hours: Mapped[int | None] = mapped_column(Integer)
    # When we received it - the moment it became knowable, and the clock
    # replay orders it by.
    local_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    market: Mapped[Market] = relationship(lazy="raise")

    __table_args__ = (
        UniqueConstraint("market_id", "local_timestamp", name="market_observed_at"),
        Index("ix_funding_observations_local_ts", "local_timestamp"),
        CheckConstraint("mark_price > 0 AND index_price > 0", name="prices_positive"),
        CheckConstraint(
            "funding_interval_hours IS NULL OR funding_interval_hours > 0",
            name="interval_positive",
        ),
    )


class MarketTrade(Base, RecordMixin):
    """Public trade print from the venue.

    Used to calibrate the slippage model against what actually traded, rather
    than inferring fills from quotes alone.
    """

    __tablename__ = "trades_market"

    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )
    # Venue trade id; unique per market so a replayed message cannot double-count.
    exchange_trade_id: Mapped[str] = mapped_column(String(64), nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    # Aggressing side, when the venue reports it.
    aggressor_side: Mapped[Side | None] = mapped_column(SIDE)

    exchange_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    local_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    market: Mapped[Market] = relationship(lazy="raise")

    __table_args__ = (
        UniqueConstraint("market_id", "exchange_trade_id", name="market_trade_id"),
        Index("ix_trades_market_market_exch_ts", "market_id", "exchange_timestamp"),
        Index("ix_trades_market_local_ts", "local_timestamp"),
        CheckConstraint("price > 0", name="price_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )
