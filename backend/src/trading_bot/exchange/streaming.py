"""The streaming boundary: what a venue must tell the market-data engine.

The engine (``trading_bot.marketdata``) owns everything generic about live data:
connections, reconnection, heartbeats, staleness and order-book synchronisation.
A venue contributes exactly two things - which WebSocket URLs carry which
markets, and how to turn one raw message into a normalized event. Keeping that
split here is what stops strategy code from ever depending on a WebSocket.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.models import (
    DepthDiff,
    MarketDataSubscription,
    MarketRef,
    Quote,
    TickerStats,
)

StreamEvent = Quote | DepthDiff | TickerStats


class StreamKind(StrEnum):
    QUOTE = "quote"  # top of book
    DEPTH = "depth"  # incremental order-book updates
    TICKER = "ticker"  # rolling 24h statistics


@dataclass(frozen=True, slots=True)
class StreamEndpoint:
    """One WebSocket connection's worth of streams."""

    name: str
    url: str
    market_type: MarketType
    streams: tuple[tuple[MarketRef, StreamKind], ...]

    @property
    def refs(self) -> tuple[MarketRef, ...]:
        return tuple(dict.fromkeys(ref for ref, _ in self.streams))


class MarketStreamSource(ABC):
    """A venue's half of live streaming."""

    venue: str

    @abstractmethod
    def endpoints(self, subscription: MarketDataSubscription) -> list[StreamEndpoint]:
        """The connections needed to stream ``subscription``.

        Venues cap streams per connection and may route stream kinds to
        different hosts, so one subscription can need several connections.
        """

    @abstractmethod
    def parse(
        self, endpoint: StreamEndpoint, raw: str | bytes, received_at: datetime
    ) -> StreamEvent | None:
        """Decode one message received on ``endpoint``.

        Returns ``None`` for control frames that carry no market data, and
        raises ``ExchangeDataError`` for anything malformed - the same
        validation contract as the REST mapping layer.
        """
