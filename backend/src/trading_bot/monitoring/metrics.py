"""Per-market statistics - the monitoring layer's output.

``MarketMetrics`` pairs a market's instantaneous state with rolling statistics
over a short window, so one wide tick cannot pass for a wide market and one slow
message cannot pass for a latency problem. Windows hold only samples taken while
the market was live: a stale quote sampled again and again would otherwise drag
every average toward it.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_bot.exchange.models import MarketRef
from trading_bot.marketdata.models import BookLiquidity, FeedStatus


class RollingWindow:
    """The most recent ``size`` values of one series."""

    __slots__ = ("_values",)

    def __init__(self, size: int) -> None:
        if size < 1:
            raise ValueError("window size must be positive")
        self._values: deque[Decimal] = deque(maxlen=size)

    def __len__(self) -> int:
        return len(self._values)

    def add(self, value: Decimal | None) -> None:
        if value is not None:
            self._values.append(value)

    def mean(self) -> Decimal | None:
        if not self._values:
            return None
        return sum(self._values, Decimal(0)) / len(self._values)

    def percentile(self, fraction: float) -> Decimal | None:
        """Nearest-rank percentile: always a value that was actually observed."""
        if not 0 < fraction <= 1:
            raise ValueError("fraction must be within (0, 1]")
        if not self._values:
            return None
        ordered = sorted(self._values)
        return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


@dataclass(frozen=True, slots=True)
class MarketMetrics:
    """Everything the monitor knows about one market at one sample."""

    ref: MarketRef
    # Pair position in the liquidity ranking; None for explicitly added markets.
    rank: int | None
    status: FeedStatus
    sampled_at: datetime
    mid_price: Decimal | None
    spread: Decimal | None
    spread_bps: Decimal | None
    spread_pct: Decimal | None
    spread_bps_mean: Decimal | None
    volume_24h: Decimal | None
    quote_volume_24h: Decimal | None
    liquidity: BookLiquidity | None
    # Within the liquidity band; None without a synchronised book.
    imbalance: Decimal | None
    imbalance_mean: Decimal | None
    # Freshness: time since any message for this market, and whether the
    # market is live - a quote present and nothing stale about it.
    age_ms: int | None
    is_fresh: bool
    latency_ms: int | None
    latency_p50_ms: int | None
    latency_p95_ms: int | None
    # Live samples currently in the window.
    samples: int


@dataclass(frozen=True, slots=True)
class MonitorSummary:
    markets: int
    fresh: int
    stale: int
    disconnected: int
    median_spread_bps: Decimal | None
    # Across monitored markets, in their quote currencies.
    quote_volume_24h: Decimal
    # Markets with a synchronised book, and of those, how many have both sides
    # of the liquidity band inside the snapshot's known range.
    books_measured: int
    books_complete: int
