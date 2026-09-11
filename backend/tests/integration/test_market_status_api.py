"""System status for Exchange and Market Data, judged from what the service wrote.

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

from trading_bot.api.market_status import market_data_status
from trading_bot.api.schemas import ComponentStatus
from trading_bot.core.config import DatabaseConfig, Settings
from trading_bot.db.models import Market, MarketData
from trading_bot.db.models.enums import MarketType
from trading_bot.db.session import dispose_engine, init_engine
from trading_bot.exchange.models import MarketRef, MarketSpec
from trading_bot.marketdata.recorder import register_markets

pytestmark = pytest.mark.requires_postgres

SYMBOL = "STATUSTESTUSDT"
SPOT = MarketRef("binance", SYMBOL, MarketType.SPOT)
PERP = MarketRef("binance", SYMBOL, MarketType.PERPETUAL)
Factory = async_sessionmaker[AsyncSession]


@pytest.fixture
async def store(postgres_url: str) -> AsyncIterator[Factory]:
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


async def select_markets(factory: Factory, *refs: MarketRef) -> dict[MarketRef, int]:
    """What the service does at start-up: record its selection."""
    specs = [
        MarketSpec(ref=ref, base_asset="STATUSTEST", quote_asset="USDT", is_active=True)
        for ref in refs
    ]
    async with factory() as session:
        ids = await register_markets(session, specs)
        await session.commit()
    return ids


async def add_quotes(
    factory: Factory, ids: dict[MarketRef, int], ages: dict[MarketRef, timedelta], now: datetime
) -> None:
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
        for ref, age in ages.items()
    ]
    async with factory() as session:
        await session.execute(insert(MarketData), rows)
        await session.commit()


class TestMarketDataStatus:
    async def test_nothing_selected_reads_offline_with_a_hint(self, store: Factory) -> None:
        status = await market_data_status(Settings(), datetime.now(UTC))
        assert status.exchange.status is ComponentStatus.OFFLINE
        assert status.market_data.status is ComponentStatus.OFFLINE
        assert "make market-data" in status.market_data.detail
        assert (status.monitored_spot, status.monitored_perpetual) == (0, 0)

    async def test_selected_but_silent_markets_read_offline(self, store: Factory) -> None:
        await select_markets(store, SPOT, PERP)
        status = await market_data_status(Settings(), datetime.now(UTC))
        assert status.market_data.status is ComponentStatus.OFFLINE
        assert "from 2 monitored markets" in status.market_data.detail
        assert (status.monitored_spot, status.monitored_perpetual) == (1, 1)

    async def test_fresh_quotes_on_every_market_read_healthy(self, store: Factory) -> None:
        now = datetime.now(UTC)
        ids = await select_markets(store, SPOT, PERP)
        await add_quotes(store, ids, {SPOT: timedelta(seconds=1), PERP: timedelta(seconds=2)}, now)
        status = await market_data_status(Settings(), now)
        assert status.exchange.status is ComponentStatus.HEALTHY
        assert status.market_data.status is ComponentStatus.HEALTHY
        assert status.market_data.detail.startswith("2/2 markets live")

    async def test_one_quiet_market_reads_degraded_and_is_named(self, store: Factory) -> None:
        now = datetime.now(UTC)
        ids = await select_markets(store, SPOT, PERP)
        await add_quotes(store, ids, {SPOT: timedelta(seconds=1)}, now)
        status = await market_data_status(Settings(), now)
        assert status.exchange.status is ComponentStatus.HEALTHY
        assert status.market_data.status is ComponentStatus.DEGRADED
        assert f"{SYMBOL} perpetual" in status.market_data.detail

    async def test_quotes_older_than_the_window_read_offline(self, store: Factory) -> None:
        now = datetime.now(UTC)
        ids = await select_markets(store, SPOT, PERP)
        old = timedelta(minutes=5)
        await add_quotes(store, ids, {SPOT: old, PERP: old}, now)
        status = await market_data_status(Settings(), now)
        assert status.exchange.status is ComponentStatus.OFFLINE
        assert status.market_data.status is ComponentStatus.OFFLINE

    async def test_markets_dropped_from_the_selection_no_longer_count(self, store: Factory) -> None:
        now = datetime.now(UTC)
        ids = await select_markets(store, SPOT, PERP)
        await select_markets(store, SPOT)  # the next start picked only the spot leg
        await add_quotes(store, ids, {SPOT: timedelta(seconds=1)}, now)
        status = await market_data_status(Settings(), now)
        assert status.market_data.status is ComponentStatus.HEALTHY
        assert (status.monitored_spot, status.monitored_perpetual) == (1, 0)


class TestUnreadableDatabase:
    async def test_no_database_reads_offline_with_unknown_counts(self) -> None:
        status = await market_data_status(Settings(), datetime.now(UTC))
        assert status.exchange.status is ComponentStatus.OFFLINE
        assert status.market_data.status is ComponentStatus.OFFLINE
        assert "cannot read" in status.market_data.detail
        assert status.monitored_spot is None
