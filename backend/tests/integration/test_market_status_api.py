"""System status for Exchange and Market Data, judged from stored quotes.

The rows must be committed - the status code reads through its own engine, as
the API does - so this module cleans up after itself rather than relying on the
rolled-back ``db`` fixture.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from trading_bot.api.market_status import market_data_components
from trading_bot.api.schemas import ComponentStatus
from trading_bot.core.config import DatabaseConfig, MarketsConfig, Settings
from trading_bot.db.models import Market, MarketData
from trading_bot.db.models.enums import MarketType
from trading_bot.db.session import dispose_engine, init_engine
from trading_bot.exchange.models import MarketRef, MarketSpec
from trading_bot.marketdata.recorder import register_markets

pytestmark = pytest.mark.requires_postgres

SYMBOL = "STATUSTESTUSDT"
SPOT = MarketRef("binance", SYMBOL, MarketType.SPOT)
PERP = MarketRef("binance", SYMBOL, MarketType.PERPETUAL)


def settings() -> Settings:
    return Settings(markets=MarketsConfig(spot_symbols=[SYMBOL], perpetual_symbols=[SYMBOL]))


@pytest.fixture
async def store(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(postgres_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    init_engine(DatabaseConfig(url_override=postgres_url))
    try:
        yield factory
    finally:
        async with factory() as session:
            # market_data rows go with their market (ON DELETE CASCADE).
            await session.execute(delete(Market).where(Market.symbol == SYMBOL))
            await session.commit()
        await dispose_engine()
        await engine.dispose()


async def add_quotes(
    factory: async_sessionmaker[AsyncSession],
    now: datetime,
    *,
    spot_age: timedelta | None,
    perp_age: timedelta | None,
) -> None:
    specs = [
        MarketSpec(ref=ref, base_asset="STATUSTEST", quote_asset="USDT", is_active=True)
        for ref in (SPOT, PERP)
    ]
    async with factory() as session:
        ids = await register_markets(session, specs)
        rows = [
            {
                "market_id": ids[ref],
                "bid": Decimal("100"),
                "ask": Decimal("100.1"),
                "bid_size": Decimal(1),
                "ask_size": Decimal(1),
                "mid_price": Decimal("100.05"),
                "spread": Decimal("0.1"),
                "spread_bps": Decimal("9.995"),
                "local_timestamp": now - age,
            }
            for ref, age in ((SPOT, spot_age), (PERP, perp_age))
            if age is not None
        ]
        await session.execute(insert(MarketData), rows)
        await session.commit()


class TestMarketDataStatus:
    async def test_no_quotes_reads_offline_with_a_hint(
        self, store: async_sessionmaker[AsyncSession]
    ) -> None:
        exchange, market_data = await market_data_components(settings(), datetime.now(UTC))
        assert exchange.status is ComponentStatus.OFFLINE
        assert market_data.status is ComponentStatus.OFFLINE
        assert "make market-data" in market_data.detail

    async def test_fresh_quotes_on_every_market_read_healthy(
        self, store: async_sessionmaker[AsyncSession]
    ) -> None:
        now = datetime.now(UTC)
        await add_quotes(store, now, spot_age=timedelta(seconds=1), perp_age=timedelta(seconds=2))
        exchange, market_data = await market_data_components(settings(), now)
        assert exchange.status is ComponentStatus.HEALTHY
        assert market_data.status is ComponentStatus.HEALTHY
        assert market_data.detail.startswith("2/2 markets live")

    async def test_one_quiet_market_reads_degraded_and_is_named(
        self, store: async_sessionmaker[AsyncSession]
    ) -> None:
        now = datetime.now(UTC)
        await add_quotes(store, now, spot_age=timedelta(seconds=1), perp_age=None)
        exchange, market_data = await market_data_components(settings(), now)
        assert exchange.status is ComponentStatus.HEALTHY
        assert market_data.status is ComponentStatus.DEGRADED
        assert f"{SYMBOL} perpetual" in market_data.detail

    async def test_quotes_older_than_the_window_read_offline(
        self, store: async_sessionmaker[AsyncSession]
    ) -> None:
        now = datetime.now(UTC)
        old = timedelta(minutes=5)
        await add_quotes(store, now, spot_age=old, perp_age=old)
        exchange, market_data = await market_data_components(settings(), now)
        assert exchange.status is ComponentStatus.OFFLINE
        assert market_data.status is ComponentStatus.OFFLINE


class TestUnreadableDatabase:
    async def test_no_database_reads_offline_never_healthy(self) -> None:
        exchange, market_data = await market_data_components(settings(), datetime.now(UTC))
        assert exchange.status is ComponentStatus.OFFLINE
        assert market_data.status is ComponentStatus.OFFLINE
        assert "cannot read" in market_data.detail
