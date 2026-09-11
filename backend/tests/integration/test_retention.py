"""Retention purges against a real database."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.factories import NOW, make_market, make_market_data, make_opportunity
from trading_bot.core.config import RetentionConfig
from trading_bot.db.models import MarketData, Opportunity
from trading_bot.db.retention import purge_expired

pytestmark = pytest.mark.requires_postgres


async def _count(db: AsyncSession, model: type) -> int:
    return await db.scalar(select(func.count()).select_from(model)) or 0


class TestPurge:
    async def test_deletes_only_rows_past_the_window(self, db: AsyncSession) -> None:
        market = make_market()
        fresh = make_market_data(market, at=NOW - timedelta(days=1))
        stale = make_market_data(market, at=NOW - timedelta(days=30))
        db.add_all([fresh, stale])
        await db.flush()

        deleted = await purge_expired(db, RetentionConfig(market_data_days=7), now=NOW)

        assert deleted["market_data"] == 1
        remaining = (await db.execute(select(MarketData))).scalars().all()
        assert [row.id for row in remaining] == [fresh.id]

    async def test_research_data_is_never_purged(self, db: AsyncSession) -> None:
        """An old opportunity is still the research record."""
        market = make_market()
        db.add(make_opportunity(market, detected_at=NOW - timedelta(days=365)))
        db.add(make_market_data(market, at=NOW - timedelta(days=365)))
        await db.flush()

        await purge_expired(db, RetentionConfig(), now=NOW)

        assert await _count(db, Opportunity) == 1
        assert await _count(db, MarketData) == 0

    async def test_disabled_retention_deletes_nothing(self, db: AsyncSession) -> None:
        market = make_market()
        db.add(make_market_data(market, at=NOW - timedelta(days=999)))
        await db.flush()

        assert await purge_expired(db, RetentionConfig(enabled=False), now=NOW) == {}
        assert await _count(db, MarketData) == 1

    async def test_batching_drains_a_backlog_larger_than_one_batch(self, db: AsyncSession) -> None:
        """With 150 expired rows and a batch of 100, the purge must loop, not stop."""
        market = make_market()
        for minute in range(150):
            db.add(make_market_data(market, at=NOW - timedelta(days=30, minutes=minute)))
        db.add(make_market_data(market, at=NOW - timedelta(hours=1)))
        await db.flush()

        deleted = await purge_expired(
            db,
            RetentionConfig(market_data_days=7, purge_batch_size=100),
            now=NOW,
        )

        assert deleted["market_data"] == 150
        # The fresh row survived.
        assert await _count(db, MarketData) == 1

    async def test_nothing_to_purge_is_not_an_error(self, db: AsyncSession) -> None:
        deleted = await purge_expired(db, RetentionConfig(), now=NOW)
        assert deleted == {"market_data": 0, "order_books": 0, "trades_market": 0}
