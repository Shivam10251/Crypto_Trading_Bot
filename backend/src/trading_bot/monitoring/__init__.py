"""Market monitoring (Phase 4): which markets to watch, and how they behave.

``select_universe`` decides what the market-data engine streams; ``MarketMonitor``
turns the engine's snapshots into per-market statistics over a rolling window.
Both use the engine's read API, never its WebSockets.
"""

from trading_bot.monitoring.metrics import MarketMetrics, MonitorSummary
from trading_bot.monitoring.monitor import MarketMonitor
from trading_bot.monitoring.universe import Universe, select_universe

__all__ = ["MarketMetrics", "MarketMonitor", "MonitorSummary", "Universe", "select_universe"]
