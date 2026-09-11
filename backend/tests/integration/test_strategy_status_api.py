"""Strategy Engine health, judged by the opportunities it recorded.

The rows must be committed - the status code reads through its own engine, as
the API does - so this module cleans up after itself rather than relying on the
rolled-back ``db`` fixture.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.integration.factories import make_market, make_opportunity
from trading_bot.api.schemas import ComponentStatus
from trading_bot.api.strategy_status import STALE_AFTER, strategy_status
from trading_bot.core.config import (
    DatabaseConfig,
    OpportunitiesConfig,
    Settings,
    StrategyConfig,
)
from trading_bot.db.models import Market, Opportunity
from trading_bot.db.models.enums import MarketType
from trading_bot.db.session import dispose_engine, init_engine

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 9, 11, 17, 0, tzinfo=UTC)
SYMBOL = "STRATSTATUSUSDT"
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
            # markets are ON DELETE RESTRICT from opportunities, so the
            # opportunities have to go first.
            ids = select(Market.id).where(Market.symbol == SYMBOL)
            await session.execute(delete(Opportunity).where(Opportunity.market_id.in_(ids)))
            await session.execute(delete(Market).where(Market.symbol == SYMBOL))
            await session.commit()
        await dispose_engine()
        await engine.dispose()


async def seed(factory: Factory, *, net_bps: str, detected_at: datetime) -> None:
    async with factory() as session:
        spot = make_market(SYMBOL, MarketType.SPOT)
        perp = make_market(SYMBOL, MarketType.PERPETUAL)
        session.add_all([spot, perp])
        await session.flush()
        session.add(make_opportunity(spot, perp, net_edge_bps=net_bps, detected_at=detected_at))
        await session.commit()


def settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


async def test_recent_opportunities_make_it_healthy(store: Factory) -> None:
    await seed(store, net_bps="-20", detected_at=NOW - timedelta(seconds=30))
    health = await strategy_status(settings(), NOW)
    assert health.status is ComponentStatus.HEALTHY
    assert "spot_perp_basis" in health.detail
    assert "1 opportunities recorded" in health.detail


async def test_only_rejections_is_still_healthy(store: Factory) -> None:
    """Detection is the deliverable; "nothing was tradeable" is not a fault."""
    await seed(store, net_bps="-45", detected_at=NOW - timedelta(seconds=10))
    health = await strategy_status(settings(), NOW)
    assert health.status is ComponentStatus.HEALTHY
    assert "0 with a positive net edge" in health.detail


async def test_a_positive_edge_is_counted(store: Factory) -> None:
    await seed(store, net_bps="12", detected_at=NOW - timedelta(seconds=10))
    health = await strategy_status(settings(), NOW)
    assert "1 with a positive net edge" in health.detail


async def test_stale_opportunities_read_offline(store: Factory) -> None:
    """A strategy that stopped recording must not read as running."""
    await seed(store, net_bps="-20", detected_at=NOW - STALE_AFTER - timedelta(minutes=1))
    health = await strategy_status(settings(), NOW)
    assert health.status is ComponentStatus.OFFLINE
    assert "make market-data" in health.detail


async def test_no_strategy_enabled_reads_offline(store: Factory) -> None:
    health = await strategy_status(settings(strategy=StrategyConfig(enabled=[])), NOW)
    assert health.status is ComponentStatus.OFFLINE
    assert "no strategy enabled" in health.detail


async def test_persistence_off_admits_the_blind_spot(store: Factory) -> None:
    """The strategy may well be running; the API simply has no way to know."""
    health = await strategy_status(settings(opportunities=OpportunitiesConfig(persist=False)), NOW)
    assert health.status is ComponentStatus.OFFLINE
    assert "cannot observe" in health.detail
