"""Real-time market data (Phase 3).

``MarketDataEngine`` turns a venue's WebSocket streams into ``MarketSnapshot``
objects. Strategies depend on the snapshot and the engine's read API, never on
the WebSocket layer underneath.
"""

from trading_bot.marketdata.engine import MarketDataEngine
from trading_bot.marketdata.models import (
    BookStatus,
    EngineHealth,
    FeedStatus,
    MarketDataEvent,
    MarketSnapshot,
)

__all__ = [
    "BookStatus",
    "EngineHealth",
    "FeedStatus",
    "MarketDataEngine",
    "MarketDataEvent",
    "MarketSnapshot",
]
