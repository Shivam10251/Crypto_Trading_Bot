"""Samples the engine on a fixed cadence and keeps per-market statistics.

Sampling rather than reacting to every message keeps the cost flat as markets
are added: a hundred markets cost a hundred snapshot reads per interval, however
busy they are. Strategies and the dashboard read ``metrics()``; nothing here
touches a WebSocket or a venue payload.
"""

from __future__ import annotations

import asyncio
import statistics
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from trading_bot.core.config import MonitoringConfig
from trading_bot.exchange.models import MarketRef
from trading_bot.marketdata.models import FeedStatus, MarketSnapshot
from trading_bot.monitoring.metrics import MarketMetrics, MonitorSummary, RollingWindow

_PERCENT_PER_BPS = Decimal(100)


class SnapshotSource(Protocol):
    """The engine's read API - all the monitor needs."""

    def snapshots(self) -> list[MarketSnapshot]: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_int(value: Decimal | None) -> int | None:
    return None if value is None else int(value)


class _Series:
    __slots__ = ("imbalance", "latency", "spread_bps")

    def __init__(self, size: int) -> None:
        self.spread_bps = RollingWindow(size)
        self.imbalance = RollingWindow(size)
        self.latency = RollingWindow(size)


class MarketMonitor:
    def __init__(
        self,
        source: SnapshotSource,
        config: MonitoringConfig,
        *,
        ranks: Mapping[MarketRef, int] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._source = source
        self._interval = config.sample_interval_ms / 1000
        self._window_size = max(1, config.window_seconds * 1000 // config.sample_interval_ms)
        self._ranks = dict(ranks or {})
        self._clock = clock
        self._series: dict[MarketRef, _Series] = {}
        self._latest: list[MarketMetrics] = []

    def sample(self) -> list[MarketMetrics]:
        """Measure every market now; the engine's order is kept."""
        now = self._clock()
        self._latest = [self._measure(snapshot, now) for snapshot in self._source.snapshots()]
        return list(self._latest)

    def metrics(self) -> list[MarketMetrics]:
        return list(self._latest)

    def summary(self) -> MonitorSummary:
        return summarise(self._latest)

    async def run(self) -> None:
        while True:
            self.sample()
            await asyncio.sleep(self._interval)

    def _measure(self, snapshot: MarketSnapshot, now: datetime) -> MarketMetrics:
        series = self._series.get(snapshot.ref)
        if series is None:
            series = self._series[snapshot.ref] = _Series(self._window_size)
        liquidity = snapshot.liquidity
        imbalance = liquidity.imbalance if liquidity is not None else None
        if snapshot.is_live:
            series.spread_bps.add(snapshot.spread_bps)
            series.imbalance.add(imbalance)
            if snapshot.latency_ms is not None:
                series.latency.add(Decimal(snapshot.latency_ms))
        spread_bps = snapshot.spread_bps
        return MarketMetrics(
            ref=snapshot.ref,
            rank=self._ranks.get(snapshot.ref),
            status=snapshot.status,
            sampled_at=now,
            mid_price=snapshot.mid_price,
            spread=snapshot.spread,
            spread_bps=spread_bps,
            spread_pct=spread_bps / _PERCENT_PER_BPS if spread_bps is not None else None,
            spread_bps_mean=series.spread_bps.mean(),
            volume_24h=snapshot.volume_24h,
            quote_volume_24h=snapshot.quote_volume_24h,
            liquidity=liquidity,
            imbalance=imbalance,
            imbalance_mean=series.imbalance.mean(),
            age_ms=snapshot.age_ms,
            is_fresh=snapshot.is_live,
            latency_ms=snapshot.latency_ms,
            latency_p50_ms=_as_int(series.latency.percentile(0.5)),
            latency_p95_ms=_as_int(series.latency.percentile(0.95)),
            samples=len(series.spread_bps),
        )


def summarise(metrics: Sequence[MarketMetrics]) -> MonitorSummary:
    spreads = [m.spread_bps for m in metrics if m.is_fresh and m.spread_bps is not None]
    books = [m.liquidity for m in metrics if m.liquidity is not None]
    return MonitorSummary(
        markets=len(metrics),
        fresh=sum(m.is_fresh for m in metrics),
        stale=sum(m.status is FeedStatus.STALE for m in metrics),
        disconnected=sum(m.status is FeedStatus.DISCONNECTED for m in metrics),
        median_spread_bps=statistics.median(spreads) if spreads else None,
        quote_volume_24h=sum(
            (m.quote_volume_24h for m in metrics if m.quote_volume_24h is not None), Decimal(0)
        ),
        books_measured=len(books),
        books_complete=sum(book.bid_complete and book.ask_complete for book in books),
    )
