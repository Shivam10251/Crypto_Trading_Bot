"""Live checks against binance.com public endpoints.

Opt-in: these hit the real API, so they are skipped unless TB_TEST_LIVE=1.
Everything here is public market data - no credentials, no orders.

Their value is catching the thing mocks cannot: Binance changing a payload
shape, or an endpoint becoming unreachable from this network.

    TB_TEST_LIVE=1 uv run pytest tests/integration/test_binance_live.py -v
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from trading_bot.core.config import ExchangeConfig
from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.binance import BinanceExchangeAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("TB_TEST_LIVE") != "1",
    reason="live Binance test; set TB_TEST_LIVE=1 to run",
)


@pytest.fixture
async def adapter() -> BinanceExchangeAdapter:
    async with BinanceExchangeAdapter(ExchangeConfig()) as live:
        yield live


class TestLiveMarketData:
    async def test_spot_quote(self, adapter: BinanceExchangeAdapter) -> None:
        quote = await adapter.get_ticker(adapter.market_ref("BTCUSDT", MarketType.SPOT))
        assert quote.bid > 0
        assert quote.ask >= quote.bid
        # Sanity bound: BTC is not worth 10 USD or 10 million.
        assert Decimal(10) < quote.mid_price < Decimal(10_000_000)
        # Confirms the documented asymmetry still holds.
        assert quote.exchange_timestamp is None

    async def test_perpetual_quote_reports_its_clock(self, adapter: BinanceExchangeAdapter) -> None:
        quote = await adapter.get_ticker(adapter.market_ref("BTCUSDT", MarketType.PERPETUAL))
        assert quote.exchange_timestamp is not None
        assert quote.latency_ms is not None

    async def test_order_book_depth(self, adapter: BinanceExchangeAdapter) -> None:
        book = await adapter.get_order_book(
            adapter.market_ref("BTCUSDT", MarketType.SPOT), levels=5
        )
        assert len(book.bids) == 5
        assert len(book.asks) == 5
        assert book.best_ask >= book.best_bid
        # A liquid book must show real size on both sides.
        assert book.depth_notional(Side.BUY) > 0
        assert book.depth_notional(Side.SELL) > 0

    async def test_funding_is_available_for_perpetuals(
        self, adapter: BinanceExchangeAdapter
    ) -> None:
        funding = await adapter.get_funding(adapter.market_ref("BTCUSDT", MarketType.PERPETUAL))
        assert funding.mark_price > 0
        # Funding rates are small; anything beyond 1% per interval is suspect.
        assert abs(funding.last_funding_rate) < Decimal("0.01")

    async def test_clock_skew_is_small(self, adapter: BinanceExchangeAdapter) -> None:
        clock = await adapter.get_server_time()
        assert abs(clock.skew_ms) < 60_000

    async def test_both_legs_of_the_basis_are_listed(self, adapter: BinanceExchangeAdapter) -> None:
        """The first strategy needs BTCUSDT on both spot and perp."""
        specs = await adapter.get_markets()
        pairs = {(spec.symbol, spec.ref.market_type) for spec in specs}
        assert ("BTCUSDT", MarketType.SPOT) in pairs
        assert ("BTCUSDT", MarketType.PERPETUAL) in pairs
