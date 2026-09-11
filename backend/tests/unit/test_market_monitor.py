"""Monitoring statistics over sampled engine snapshots."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from decimal import Decimal

from trading_bot.core.config import MonitoringConfig
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.models import MarketRef, Quote
from trading_bot.marketdata.models import BookLiquidity, BookStatus, FeedStatus, MarketSnapshot
from trading_bot.monitoring.monitor import MarketMonitor

NOW = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
BTC = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
ETH = MarketRef("binance", "ETHUSDT", MarketType.SPOT)
SOL = MarketRef("binance", "SOLUSDT", MarketType.PERPETUAL)
XRP = MarketRef("binance", "XRPUSDT", MarketType.SPOT)
CONFIG = MonitoringConfig(sample_interval_ms=1000, window_seconds=5)  # five samples


def liquidity(bid: str, ask: str, *, complete: bool = True) -> BookLiquidity:
    return BookLiquidity(
        band_bps=Decimal(10),
        bid_notional=Decimal(bid),
        ask_notional=Decimal(ask),
        bid_complete=complete,
        ask_complete=True,
        reference_notional=Decimal(10_000),
        buy_slippage_bps=Decimal("0.5"),
        sell_slippage_bps=None,
    )


def snapshot(
    ref: MarketRef = BTC,
    *,
    bid: str = "99.95",
    ask: str = "100.05",
    status: FeedStatus = FeedStatus.LIVE,
    latency: int | None = None,
    book: BookLiquidity | None = None,
    quote_volume: str | None = None,
) -> MarketSnapshot:
    quote = Quote(
        ref=ref,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=Decimal(1),
        ask_size=Decimal(1),
        local_timestamp=NOW,
    )
    return MarketSnapshot(
        ref=ref,
        status=status,
        quote=quote,
        book=None,
        book_status=BookStatus.SYNCED if book else BookStatus.SYNCING,
        last_price=None,
        volume_24h=None,
        quote_volume_24h=Decimal(quote_volume) if quote_volume else None,
        latency_ms=latency,
        last_update_at=NOW,
        age_ms=5,
        updates=1,
        gaps=0,
        resyncs=0,
        liquidity=book,
    )


class Feed:
    """Stands in for the engine: whatever snapshots the test sets."""

    def __init__(self, *snapshots: MarketSnapshot) -> None:
        self.current = list(snapshots)

    def snapshots(self) -> list[MarketSnapshot]:
        return list(self.current)


class TestInstantaneous:
    def test_spread_in_bps_and_percent(self) -> None:
        [metrics] = MarketMonitor(Feed(snapshot()), CONFIG, ranks={BTC: 1}).sample()
        assert metrics.mid_price == Decimal(100)
        assert metrics.spread_bps == Decimal(10)
        assert metrics.spread_pct == Decimal("0.1")
        assert metrics.rank == 1
        assert metrics.is_fresh
        assert metrics.age_ms == 5

    def test_imbalance_comes_from_band_liquidity(self) -> None:
        [metrics] = MarketMonitor(Feed(snapshot(book=liquidity("300", "100"))), CONFIG).sample()
        assert metrics.imbalance == Decimal("0.5")

    def test_without_a_book_there_is_no_imbalance(self) -> None:
        [metrics] = MarketMonitor(Feed(snapshot()), CONFIG).sample()
        assert metrics.liquidity is None
        assert metrics.imbalance is None

    def test_markets_keep_the_engine_order(self) -> None:
        metrics = MarketMonitor(Feed(snapshot(ETH), snapshot(BTC)), CONFIG).sample()
        assert [m.ref for m in metrics] == [ETH, BTC]
        assert all(m.rank is None for m in metrics)


class TestWindows:
    def test_means_cover_live_samples_only(self) -> None:
        feed = Feed()
        monitor = MarketMonitor(feed, CONFIG)
        for bid, ask, status in (
            ("99.95", "100.05", FeedStatus.LIVE),  # 10 bps
            ("99.9", "100.1", FeedStatus.LIVE),  # 20 bps
            ("95", "105", FeedStatus.STALE),  # 1000 bps, but stale
        ):
            feed.current = [snapshot(bid=bid, ask=ask, status=status)]
            [metrics] = monitor.sample()
        assert metrics.spread_bps == Decimal(1000)  # still reported as it is...
        assert metrics.spread_bps_mean == Decimal(15)  # ...but kept out of the mean
        assert metrics.samples == 2
        assert not metrics.is_fresh

    def test_the_window_forgets_old_samples(self) -> None:
        feed = Feed()
        monitor = MarketMonitor(feed, CONFIG)
        for bid, ask in [("99.95", "100.05")] * 2 + [("99.9", "100.1")] * 5:
            feed.current = [snapshot(bid=bid, ask=ask)]
            [metrics] = monitor.sample()
        assert metrics.spread_bps_mean == Decimal(20)
        assert metrics.samples == 5

    def test_latency_percentiles_are_observed_values(self) -> None:
        feed = Feed()
        monitor = MarketMonitor(feed, CONFIG)
        for latency in (10, 20, 30, 40, 200):
            feed.current = [snapshot(latency=latency)]
            [metrics] = monitor.sample()
        assert (metrics.latency_p50_ms, metrics.latency_p95_ms) == (30, 200)
        assert metrics.latency_ms == 200

    def test_imbalance_mean(self) -> None:
        feed = Feed()
        monitor = MarketMonitor(feed, CONFIG)
        for book in (liquidity("300", "100"), liquidity("100", "300")):
            feed.current = [snapshot(book=book)]
            [metrics] = monitor.sample()
        assert metrics.imbalance_mean == Decimal(0)


class TestSummary:
    def test_counts_medians_and_totals(self) -> None:
        feed = Feed(
            snapshot(BTC, quote_volume="1000", book=liquidity("1", "1")),
            snapshot(
                ETH,
                bid="99.9",
                ask="100.1",
                quote_volume="500",
                book=liquidity("1", "1", complete=False),
            ),
            snapshot(SOL, status=FeedStatus.STALE),
            snapshot(XRP, status=FeedStatus.DISCONNECTED),
        )
        monitor = MarketMonitor(feed, CONFIG)
        monitor.sample()
        summary = monitor.summary()
        assert (summary.markets, summary.fresh, summary.stale, summary.disconnected) == (4, 2, 1, 1)
        assert summary.median_spread_bps == Decimal(15)  # fresh markets only
        assert summary.quote_volume_24h == Decimal(1500)
        assert (summary.books_measured, summary.books_complete) == (2, 1)

    def test_before_the_first_sample_there_is_nothing_to_summarise(self) -> None:
        summary = MarketMonitor(Feed(snapshot()), CONFIG).summary()
        assert summary.markets == 0
        assert summary.median_spread_bps is None


class TestRunning:
    async def test_run_samples_until_cancelled(self) -> None:
        config = MonitoringConfig(sample_interval_ms=100, window_seconds=5)
        monitor = MarketMonitor(Feed(snapshot()), config)
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0)
        assert len(monitor.metrics()) == 1
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
