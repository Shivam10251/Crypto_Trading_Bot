"""The internal market-data model - what the rest of the platform consumes.

``MarketSnapshot`` is everything known about one market at one moment: the
current top of book, a synchronised depth view, rolling volume, latency and
freshness. Strategies, the monitor and the risk engine read these and never see
a WebSocket, a venue payload or a connection state machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from trading_bot.db.models.enums import Severity, SystemEventType
from trading_bot.exchange.models import MarketRef, OrderBook, Quote


class FeedStatus(StrEnum):
    """Whether a market's data can be acted on."""

    CONNECTING = "CONNECTING"  # subscribed, nothing received yet
    LIVE = "LIVE"
    STALE = "STALE"  # connected, but nothing arrived within the window
    DISCONNECTED = "DISCONNECTED"  # a connection carrying this market is down


class BookStatus(StrEnum):
    DISABLED = "DISABLED"  # depth not subscribed
    SYNCING = "SYNCING"  # being rebuilt from a snapshot; no depth published
    SYNCED = "SYNCED"


@dataclass(frozen=True, slots=True)
class BookLiquidity:
    """Resting liquidity near the mid, from every level the local book knows."""

    band_bps: Decimal
    # Quote-currency value resting within band_bps of the mid, per side.
    bid_notional: Decimal
    ask_notional: Decimal
    # False when the band reaches past the price range the snapshot covered:
    # the figure is then a lower bound, not a measurement.
    bid_complete: bool
    ask_complete: bool
    reference_notional: Decimal
    # Average fill distance from the mid, in bps, of a market order worth
    # reference_notional; None when the known book cannot fill it.
    buy_slippage_bps: Decimal | None
    sell_slippage_bps: Decimal | None

    @property
    def imbalance(self) -> Decimal:
        """Bid minus ask value within the band, in [-1, 1]; positive leans bid."""
        total = self.bid_notional + self.ask_notional
        if total == 0:
            return Decimal(0)
        return (self.bid_notional - self.ask_notional) / total


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """One market, now. Immutable, so it can be handed to any consumer."""

    ref: MarketRef
    status: FeedStatus
    quote: Quote | None
    # Top levels of the synchronised local book; None unless SYNCED.
    book: OrderBook | None
    book_status: BookStatus
    last_price: Decimal | None
    # Rolling 24h volume in base and quote asset; None until the first ticker.
    volume_24h: Decimal | None
    quote_volume_24h: Decimal | None
    # Exchange -> local delay of the most recent message that carried a venue
    # clock. For Binance spot that is a depth or ticker event, never the quote.
    latency_ms: int | None
    # Local receipt time of the most recent message of any kind.
    last_update_at: datetime | None
    age_ms: int | None
    updates: int
    # Order-book integrity failures (sequence gaps, crossed books) and rebuilds.
    gaps: int
    resyncs: int
    # Measured from the full synchronised book; None unless SYNCED.
    liquidity: BookLiquidity | None = None

    @property
    def is_live(self) -> bool:
        return self.status is FeedStatus.LIVE and self.quote is not None

    @property
    def best_bid(self) -> Decimal | None:
        return self.quote.bid if self.quote else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.quote.ask if self.quote else None

    @property
    def bid_size(self) -> Decimal | None:
        return self.quote.bid_size if self.quote else None

    @property
    def ask_size(self) -> Decimal | None:
        return self.quote.ask_size if self.quote else None

    @property
    def mid_price(self) -> Decimal | None:
        return self.quote.mid_price if self.quote else None

    @property
    def spread(self) -> Decimal | None:
        return self.quote.spread if self.quote else None

    @property
    def spread_bps(self) -> Decimal | None:
        return self.quote.spread_bps if self.quote else None

    @property
    def exchange_timestamp(self) -> datetime | None:
        return self.quote.exchange_timestamp if self.quote else None

    @property
    def local_timestamp(self) -> datetime | None:
        return self.quote.local_timestamp if self.quote else None


@dataclass(frozen=True, slots=True)
class MarketDataEvent:
    """An infrastructure event worth an operator's attention and an audit row."""

    event_type: SystemEventType
    severity: Severity
    message: str
    occurred_at: datetime
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EngineHealth:
    connections_up: int
    connections_total: int
    markets_live: int
    markets_total: int
    books_synced: int
    books_total: int
    invalid_messages: int
