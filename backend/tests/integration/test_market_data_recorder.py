"""Recorder persistence against PostgreSQL: upserts, sampling and audit events."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.db.models import Market, MarketData, SystemEvent
from trading_bot.db.models.enums import MarketType, Severity, SystemEventType
from trading_bot.exchange.models import MarketRef, MarketSpec, Quote
from trading_bot.marketdata.models import BookStatus, FeedStatus, MarketDataEvent, MarketSnapshot
from trading_bot.marketdata.recorder import MarketDataRecorder, register_markets

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def spec(ref: MarketRef, **overrides: Any) -> MarketSpec:
    values: dict[str, Any] = {
        "ref": ref,
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "is_active": True,
        "tick_size": Decimal("0.01"),
        "step_size": Decimal("0.00001"),
        "min_notional": Decimal("5"),
    }
    return MarketSpec(**{**values, **overrides})


def quote(
    ref: MarketRef,
    bid: str = "77280.00",
    ask: str = "77280.01",
    *,
    sequence: int = 1,
    exchange_timestamp: datetime | None = None,
) -> Quote:
    return Quote(
        ref=ref,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=Decimal("0.5"),
        ask_size=Decimal("1.5"),
        local_timestamp=NOW,
        exchange_timestamp=exchange_timestamp,
        sequence=sequence,
    )


def snapshot(ref: MarketRef, q: Quote | None) -> MarketSnapshot:
    return MarketSnapshot(
        ref=ref,
        status=FeedStatus.LIVE,
        quote=q,
        book=None,
        book_status=BookStatus.DISABLED,
        last_price=None,
        volume_24h=Decimal("15245.41532"),
        quote_volume_24h=None,
        latency_ms=None,
        last_update_at=NOW,
        age_ms=0,
        updates=1,
        gaps=0,
        resyncs=0,
    )


def using(session: AsyncSession) -> SessionFactory:
    """A session factory over the test's rolled-back session."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield session

    return factory


GAP = MarketDataEvent(
    SystemEventType.DATA_GAP,
    Severity.WARNING,
    "order book invalidated: gap",
    NOW,
    {"market": str(SPOT)},
)


class TestRegisterMarkets:
    async def test_upsert_is_idempotent_and_refreshes_filters(self, db: AsyncSession) -> None:
        perp = spec(PERP, contract_size=Decimal(1), settlement_asset="USDT")
        first = await register_markets(db, [spec(SPOT), perp])
        second = await register_markets(db, [spec(SPOT, tick_size=Decimal("0.1"))])
        assert set(first) == {SPOT, PERP}
        assert second[SPOT] == first[SPOT]
        count = await db.scalar(
            select(func.count()).select_from(Market).where(Market.symbol == "BTCUSDT")
        )
        assert count == 2
        tick = await db.scalar(select(Market.tick_size).where(Market.id == first[SPOT]))
        assert tick == Decimal("0.1")

    async def test_missing_fees_do_not_erase_recorded_ones(self, db: AsyncSession) -> None:
        ids = await register_markets(db, [spec(SPOT, taker_fee_bps=Decimal("7.5"))])
        await register_markets(db, [spec(SPOT)])
        fee = await db.scalar(select(Market.taker_fee_bps).where(Market.id == ids[SPOT]))
        assert fee == Decimal("7.5")

    async def test_registration_records_exactly_one_selection(self, db: AsyncSession) -> None:
        """is_monitored describes the latest selection, never a leftover."""
        first = await register_markets(db, [spec(SPOT), spec(PERP)])
        await register_markets(db, [spec(PERP)])
        rows = await db.execute(
            select(Market.id, Market.is_monitored).where(Market.id.in_(first.values()))
        )
        assert dict(rows.tuples().all()) == {first[SPOT]: False, first[PERP]: True}


class TestSampling:
    async def test_only_changed_quotes_are_written(self, db: AsyncSession) -> None:
        ids = await register_markets(db, [spec(SPOT), spec(PERP)])
        recorder = MarketDataRecorder(ids, using(db), interval_seconds=1)
        spot, perp = quote(SPOT), quote(PERP)
        assert await recorder.flush([snapshot(SPOT, spot), snapshot(PERP, perp)]) == 2
        # Nothing new arrived, so nothing is written - no duplicated observations.
        assert await recorder.flush([snapshot(SPOT, spot), snapshot(PERP, perp)]) == 0
        newer = quote(SPOT, "77281.00", "77281.01", sequence=2)
        assert await recorder.flush([snapshot(SPOT, newer), snapshot(PERP, perp)]) == 1
        assert await db.scalar(select(func.count()).select_from(MarketData)) == 3

    async def test_rows_keep_the_venue_clock_honest(self, db: AsyncSession) -> None:
        ids = await register_markets(db, [spec(SPOT), spec(PERP)])
        recorder = MarketDataRecorder(ids, using(db), interval_seconds=1)
        venue_time = NOW - timedelta(milliseconds=40)
        await recorder.flush(
            [
                snapshot(SPOT, quote(SPOT)),
                snapshot(PERP, quote(PERP, exchange_timestamp=venue_time)),
            ]
        )
        spot_row, perp_row = (
            (await db.execute(select(MarketData).order_by(MarketData.id))).scalars().all()
        )
        assert spot_row.exchange_timestamp is None
        assert spot_row.latency_ms is None
        assert perp_row.exchange_timestamp == venue_time
        assert perp_row.latency_ms == 40
        assert spot_row.mid_price == Decimal("77280.005")
        assert spot_row.volume_24h == Decimal("15245.41532")
        assert spot_row.sequence == 1

    async def test_unregistered_or_quoteless_markets_are_skipped(self, db: AsyncSession) -> None:
        ids = await register_markets(db, [spec(SPOT)])
        recorder = MarketDataRecorder(ids, using(db), interval_seconds=1)
        assert await recorder.flush([snapshot(SPOT, None), snapshot(PERP, quote(PERP))]) == 0


class TestEvents:
    async def test_events_become_system_event_rows_once(self, db: AsyncSession) -> None:
        recorder = MarketDataRecorder({}, using(db), interval_seconds=1)
        recorder.record_event(GAP)
        await recorder.flush([])
        await recorder.flush([])
        row = (await db.execute(select(SystemEvent))).scalar_one()
        assert row.component == "market_data"
        assert row.event_type is SystemEventType.DATA_GAP
        assert row.severity is Severity.WARNING
        assert row.context == {"market": "binance:BTCUSDT:SPOT"}

    async def test_events_survive_a_failed_write(self, db: AsyncSession) -> None:
        database_up = False

        @asynccontextmanager
        async def flaky() -> AsyncIterator[AsyncSession]:
            if not database_up:
                raise ConnectionError("database down")
            yield db

        recorder = MarketDataRecorder({}, flaky, interval_seconds=1)
        recorder.record_event(GAP)
        assert await recorder.flush([]) == 0
        assert recorder.failures == 1
        database_up = True
        await recorder.flush([])
        assert await db.scalar(select(func.count()).select_from(SystemEvent)) == 1
