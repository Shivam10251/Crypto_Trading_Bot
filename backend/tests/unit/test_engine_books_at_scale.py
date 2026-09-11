"""Engine order books at monitoring scale: liquidity views and snapshot throttling.

Uses the fake venue and helpers of ``test_market_data_engine``.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from tests.unit.test_market_data_engine import (
    SPOT,
    Clock,
    FakeVenue,
    book_ticker,
    depth_update,
    eventually,
    fast_config,
    ladder,
    running,
)
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.binance.streams import BinanceStreamSource
from trading_bot.exchange.models import MarketDataSubscription, MarketRef, OrderBook
from trading_bot.marketdata import FeedStatus, MarketDataEngine


class TestBookViews:
    async def test_a_synced_book_reports_its_liquidity(self) -> None:
        subscription = MarketDataSubscription(refs=(SPOT,), include_depth=True, depth_levels=2)
        config = fast_config(liquidity_band_bps=200, reference_order_notional=50)
        async with running(subscription, snapshots={SPOT: [ladder(SPOT, 100)]}, config=config) as h:
            await h.venue.wait_connected("spot")
            h.venue.send("spot", depth_update(SPOT, 101, 101))
            await eventually(lambda: h.engine.snapshot(SPOT).liquidity is not None)
            liquidity = h.engine.snapshot(SPOT).liquidity
        assert liquidity is not None
        # mid 100, band ±2: bids 99 and 98, asks 101 and 102, one unit each.
        assert (liquidity.bid_notional, liquidity.ask_notional) == (Decimal(197), Decimal(203))
        assert liquidity.bid_complete
        assert liquidity.ask_complete
        assert liquidity.buy_slippage_bps == Decimal(100)

    async def test_a_book_being_rebuilt_reports_no_liquidity(self) -> None:
        subscription = MarketDataSubscription(refs=(SPOT,), include_depth=True, depth_levels=2)
        async with running(subscription, snapshots={SPOT: [ladder(SPOT, 100)]}) as h:
            await h.venue.wait_connected("spot")
            assert h.engine.snapshot(SPOT).liquidity is None


class TestQuietMarkets:
    async def test_a_quiet_market_on_a_busy_connection_stays_live(self) -> None:
        """Measured live: mid-cap spot markets go seconds without a message.

        The venue pushes every change, so silence on one market while its
        connection is busy means unchanged, not stale - until silence runs so
        long that the market's own stream may have died.
        """
        quiet = MarketRef("binance", "ETHUSDT", MarketType.SPOT)
        subscription = MarketDataSubscription.top_of_book(SPOT, quiet)
        async with running(subscription) as h:
            await h.venue.wait_connected("spot")
            h.venue.send("spot", book_ticker(quiet, 1, "2500", "2500.1"))
            h.venue.send("spot", book_ticker(SPOT, 1, "100", "101"))
            await eventually(lambda: h.engine.snapshot(quiet).is_live)

            h.clock.advance(2500)  # past the 2 s connection window
            h.venue.send("spot", book_ticker(SPOT, 2, "100", "101"))
            await h.venue.drained("spot")
            assert h.engine.snapshot(quiet).status is FeedStatus.LIVE

            h.clock.advance(28_000)  # 30.5 s since the quiet market's last message
            h.venue.send("spot", book_ticker(SPOT, 3, "100", "101"))
            await h.venue.drained("spot")
            assert h.engine.snapshot(quiet).status is FeedStatus.STALE
            assert h.engine.snapshot(SPOT).status is FeedStatus.LIVE


class TestThrottling:
    async def test_snapshot_requests_are_capped_in_flight(self) -> None:
        """Fifty books rebuilding at once must not become fifty requests at once."""
        refs = tuple(
            MarketRef("binance", symbol, MarketType.SPOT)
            for symbol in ("AAAUSDT", "BBBUSDT", "CCCUSDT")
        )
        in_flight = peak = 0

        async def slow_fetch(ref: MarketRef, levels: int) -> OrderBook:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return ladder(ref, 100)

        venue = FakeVenue()
        engine = MarketDataEngine(
            BinanceStreamSource(),
            MarketDataSubscription(refs=refs, include_depth=True, depth_levels=2),
            fast_config(max_concurrent_snapshots=1),
            snapshot_fetcher=slow_fetch,
            connector=venue.connector,
            clock=Clock(),
        )
        async with engine:
            await venue.wait_connected("spot")
            for ref in refs:
                venue.send("spot", depth_update(ref, 101, 101))
            await eventually(lambda: engine.health().books_synced == 3)
        assert peak == 1
