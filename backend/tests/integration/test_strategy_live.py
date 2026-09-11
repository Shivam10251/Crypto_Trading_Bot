"""The strategy against the live venue.

Opt-in: these hit the real API and stream real markets, so they are skipped
unless TB_TEST_LIVE=1. Public market data only - no credentials, no orders.

Their value is the thing fixtures cannot check: that the basis the strategy
computes matches the basis actually quoted, and that the conclusion it draws
about the live market is the honest one.

    TB_TEST_LIVE=1 uv run pytest tests/integration/test_strategy_live.py -v
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.core.config import (
    CostsConfig,
    ExchangeConfig,
    MarketDataConfig,
    MarketsConfig,
    SpotPerpBasisConfig,
)
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.binance import BinanceExchangeAdapter
from trading_bot.exchange.models import MarketDataSubscription
from trading_bot.marketdata import MarketDataEngine
from trading_bot.marketdata.funding import FundingTracker
from trading_bot.monitoring.universe import select_universe
from trading_bot.strategy.base import StrategyContext
from trading_bot.strategy.basis import SpotPerpBasisStrategy
from trading_bot.strategy.costs import ConfiguredCostModel
from trading_bot.strategy.runner import StrategyRunner

pytestmark = pytest.mark.skipif(
    os.environ.get("TB_TEST_LIVE") != "1",
    reason="live Binance test; set TB_TEST_LIVE=1 to run",
)

SYMBOLS = MarketsConfig(
    selection="explicit",
    spot_symbols=["BTCUSDT", "ETHUSDT"],
    perpetual_symbols=["BTCUSDT", "ETHUSDT"],
)


@pytest.fixture
async def adapter() -> BinanceExchangeAdapter:
    async with BinanceExchangeAdapter(ExchangeConfig()) as live:
        yield live


class TestLiveFunding:
    async def test_the_venue_mixes_funding_intervals(self, adapter: BinanceExchangeAdapter) -> None:
        """The finding Phase 5 was built around: 8h is not universal."""
        intervals = await adapter.get_funding_intervals(MarketType.PERPETUAL)
        assert len(intervals) > 100
        assert set(intervals.values()) - {8}, "expected intervals other than 8h"

    async def test_bulk_rates_cover_the_venue(self, adapter: BinanceExchangeAdapter) -> None:
        rates = await adapter.get_funding_rates(MarketType.PERPETUAL)
        assert len(rates) > 100
        for rate in rates[:20]:
            assert rate.mark_price > 0
            assert abs(rate.last_funding_rate) < Decimal("0.05")

    async def test_the_tracker_keeps_only_monitored_markets(
        self, adapter: BinanceExchangeAdapter
    ) -> None:
        universe = await select_universe(adapter, SYMBOLS)
        tracker = FundingTracker(adapter, universe.refs)
        updated = await tracker.refresh()
        assert updated == 2  # the two perpetual legs, not the spot ones
        for info in tracker.rates.values():
            assert info.funding_interval_hours in (1, 2, 4, 8)


class TestLiveBasis:
    async def test_the_strategy_sees_the_basis_the_venue_quotes(
        self, adapter: BinanceExchangeAdapter
    ) -> None:
        """End to end: stream both legs, pair them, and price the real basis."""
        universe = await select_universe(adapter, SYMBOLS)
        config = MarketDataConfig(depth_levels=20, snapshot_depth=1000)
        engine = MarketDataEngine(
            adapter.stream_source(),
            MarketDataSubscription(
                refs=universe.refs, include_depth=True, depth_levels=config.depth_levels
            ),
            config,
            snapshot_fetcher=adapter.get_order_book,
        )
        tracker = FundingTracker(adapter, universe.refs)
        await tracker.refresh()

        context = StrategyContext(
            cost_model=ConfiguredCostModel(CostsConfig(), funding_horizon=timedelta(hours=1)),
            specs={spec.ref: spec for spec in universe.specs},
        )
        runner = StrategyRunner([SpotPerpBasisStrategy(SpotPerpBasisConfig())], context)
        runner.set_funding(tracker.rates)

        async with engine:
            # Give both books time to synchronise before judging anything.
            deadline = datetime.now(UTC) + timedelta(seconds=45)
            evaluation = None
            while datetime.now(UTC) < deadline:
                await asyncio.sleep(1)
                evaluation = runner.evaluate(engine.snapshots())[0]
                if evaluation.stats and evaluation.stats.pairs_usable == 2:
                    break

        assert evaluation is not None
        stats = evaluation.stats
        assert stats is not None and stats.pairs_seen == 2
        assert stats.pairs_usable == 2, f"books never synchronised: {stats.unusable}"

        for item in evaluation.opportunities:
            opportunity = item.opportunity
            # A real spot/perp basis is small. Anything above 1% on BTC or ETH
            # means the legs were mispaired, not that the market gave us 100 bps.
            assert opportunity.gross_edge_bps < Decimal(100)
            assert opportunity.notional_usd > 0
            # Every leg must have been priced off a book we could actually hit.
            assert opportunity.buy.executable_price > 0
            assert opportunity.sell.executable_price > 0
            # Funding was known for both, so the edge is complete.
            assert item.edge is not None, "funding interval missing for a top-2 market"

    async def test_taker_fees_dominate_the_live_basis(
        self, adapter: BinanceExchangeAdapter
    ) -> None:
        """The measured conclusion: BTC/ETH basis does not survive taker costs.

        If this ever fails, the market has changed and the result is worth
        looking at rather than asserting away.
        """
        universe = await select_universe(adapter, SYMBOLS)
        quotes = {}
        for spec in universe.specs:
            quotes[spec.ref] = await adapter.get_ticker(spec.ref)

        costs = CostsConfig()
        round_trip = Decimal(
            str((costs.spot_taker_fee_bps + costs.perp_taker_fee_bps) * 2 + costs.safety_buffer_bps)
        )
        for symbol in ("BTCUSDT", "ETHUSDT"):
            spot = quotes[adapter.market_ref(symbol, MarketType.SPOT)]
            perp = quotes[adapter.market_ref(symbol, MarketType.PERPETUAL)]
            basis_bps = abs(perp.mid_price - spot.mid_price) / spot.mid_price * Decimal(10_000)
            assert basis_bps < round_trip, (
                f"{symbol} basis {basis_bps:.2f} bps now exceeds the "
                f"{round_trip} bps round-trip cost floor - worth investigating"
            )
