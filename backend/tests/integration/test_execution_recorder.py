"""Simulated orders and fills reaching the execution record.

PostgreSQL is the point again: the unique index that refuses a duplicate
order, the generated ids that fills hang off, and the columns that keep a
measurement probe from being counted as a trade the strategy asked for.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.factories import make_market
from tests.unit.test_execution_coordinator import Adapter, opportunity, signal
from trading_bot.db.models import Fill, Order, Position
from trading_bot.db.models.enums import ExecutionMode, MarketType, OrderStatus, Side
from trading_bot.exchange.models import MarketRef
from trading_bot.execution.coordinator import ExecutionCoordinator
from trading_bot.execution.recorder import ExecutionRecorder, order_row, summarise

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)


@pytest.fixture
async def market_ids(db: AsyncSession) -> dict[MarketRef, int]:
    spot = make_market("BTCUSDT", MarketType.SPOT)
    perp = make_market("BTCUSDT", MarketType.PERPETUAL)
    db.add_all([spot, perp])
    await db.flush()
    return {SPOT: spot.id, PERP: perp.id}


def session_factory(db: AsyncSession):  # type: ignore[no-untyped-def]
    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield db

    return factory


async def record(
    db: AsyncSession,
    market_ids: dict[MarketRef, int],
    *,
    adapter: Adapter | None = None,
    is_shadow: bool = False,
    opportunity_uid: uuid.UUID | None = None,
) -> ExecutionRecorder:
    coordinator = ExecutionCoordinator(adapter or Adapter())
    attempt = await coordinator.execute(signal(opportunity()), is_shadow=is_shadow)
    assert attempt is not None
    recorder = ExecutionRecorder(market_ids, session_factory(db), interval_seconds=1)
    recorder.record(attempt, opportunity_uid)
    await recorder.flush()
    return recorder


class TestOrdersAndFills:
    async def test_both_legs_become_orders_with_their_fills(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        recorder = await record(db, market_ids)
        assert recorder.orders_written == 2
        orders = (await db.execute(select(Order).order_by(Order.side))).scalars().all()
        assert {o.side for o in orders} == {Side.BUY, Side.SELL}
        assert all(o.mode is ExecutionMode.PAPER for o in orders)
        assert all(o.status is OrderStatus.FILLED for o in orders)
        fills = (await db.execute(select(Fill))).scalars().all()
        assert len(fills) == 2
        assert {f.order_id for f in fills} == {o.id for o in orders}

    async def test_a_fill_attaches_to_its_own_order_in_a_batch(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """Without sort_by_parameter_order this fails silently, only in batches."""
        await record(db, market_ids, adapter=Adapter(spot="1", perpetual="0.4"))
        rows = (
            await db.execute(
                select(Order.side, Fill.quantity).join(Fill, Fill.order_id == Order.id)
            )
        ).all()
        by_side = dict(rows)
        assert by_side[Side.BUY] == Decimal(1)
        assert by_side[Side.SELL] == Decimal("0.4")

    async def test_a_simulated_fill_carries_no_venue_id(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """Inventing one would make a paper fill look like a real one."""
        await record(db, market_ids)
        fills = (await db.execute(select(Fill))).scalars().all()
        assert fills
        assert all(f.exchange_fill_id is None for f in fills)

    async def test_fill_evidence_and_open_positions_are_durable(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        await record(db, market_ids)
        orders = (await db.execute(select(Order))).scalars().all()
        fills = (await db.execute(select(Fill))).scalars().all()
        positions = (await db.execute(select(Position))).scalars().all()
        assert len(positions) == 2
        assert all(fill.position_id is not None for fill in fills)
        assert all(fill.fill_index == 0 for fill in fills)
        assert all(fill.levels == [] for fill in fills)  # fixture adapter has no book levels
        assert all(order.execution_intent_id is not None for order in orders)
        assert all(order.attempt_id is not None for order in orders)
        assert all(order.expected_price is not None for order in orders)
        assert all(order.evidence is not None for order in orders)

    async def test_an_order_that_filled_nothing_is_stored_with_its_reason(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """A dataset of only successful fills cannot say how often a leg misses."""
        await record(db, market_ids, adapter=Adapter(perpetual=None))
        missed = (
            (await db.execute(select(Order).where(Order.market_id == market_ids[PERP])))
            .scalars()
            .one()
        )
        assert missed.status is OrderStatus.REJECTED
        assert missed.filled_quantity == 0
        assert missed.average_fill_price is None
        assert missed.rejection_reason is not None
        assert "NO_LIQUIDITY" in missed.rejection_reason


class TestProvenance:
    async def test_an_order_names_the_opportunity_it_came_from(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """The episode's uid is fixed when it opens, so it is available early.

        ``signal_id`` cannot be: signals are written when the episode closes,
        which is after the order was placed.
        """
        episode_uid = uuid.uuid4()
        await record(db, market_ids, opportunity_uid=episode_uid)
        orders = (await db.execute(select(Order))).scalars().all()
        assert all(o.opportunity_uid == episode_uid for o in orders)

    async def test_a_shadow_probe_is_separable_from_a_strategy_trade(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """Counting a probe as a trade would answer the wrong question.

        The strategy explicitly declined these; including them in "how did the
        strategy do?" reports trades it refused to make.
        """
        await record(db, market_ids, is_shadow=True)
        await record(db, market_ids)
        probes = (await db.execute(select(Order).where(Order.is_shadow.is_(True)))).scalars().all()
        real = (await db.execute(select(Order).where(Order.is_shadow.is_(False)))).scalars().all()
        assert len(probes) == 2
        assert len(real) == 2
        positions = (await db.execute(select(Position))).scalars().all()
        assert sum(position.is_shadow for position in positions) == 2
        assert sum(not position.is_shadow for position in positions) == 2

    async def test_legs_of_one_attempt_share_a_client_order_id_prefix(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        await record(db, market_ids)
        orders = (await db.execute(select(Order))).scalars().all()
        prefixes = {o.client_order_id.split("-")[0] for o in orders}
        assert len(prefixes) == 1


class TestDurability:
    async def test_the_database_refuses_a_duplicate_order(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """Duplicate protection is a constraint, not a code path.

        A retried flush presents the same client_order_id, so if the first
        attempt did land the second cannot become a second position.
        """
        coordinator = ExecutionCoordinator(Adapter())
        attempt = await coordinator.execute(signal(opportunity()))
        assert attempt is not None
        recorder = ExecutionRecorder(market_ids, session_factory(db), interval_seconds=1)
        recorder.record(attempt)
        assert await recorder.flush() == 2

        duplicate = order_row(attempt.buy, market_ids, opportunity_uid=None, is_shadow=False)
        assert duplicate is not None
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(insert(Order).values(duplicate))

    async def test_retrying_the_same_attempt_converges_without_duplicate_fills(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        coordinator = ExecutionCoordinator(Adapter())
        attempt = await coordinator.execute(
            signal(opportunity()), execution_intent_id="signal:durable-episode"
        )
        assert attempt is not None
        recorder = ExecutionRecorder(market_ids, session_factory(db), interval_seconds=1)
        recorder.record(attempt)
        assert await recorder.flush() == 2
        recorder.record(attempt)
        assert await recorder.flush() == 2
        assert len((await db.execute(select(Order))).scalars().all()) == 2
        assert len((await db.execute(select(Fill))).scalars().all()) == 2
        assert len((await db.execute(select(Position))).scalars().all()) == 2

    async def test_an_unknown_market_is_skipped_not_guessed(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        recorder = await record(db, {})
        assert recorder.orders_written == 0
        assert (await db.execute(select(Order))).scalars().all() == []

    async def test_unhedged_attempts_are_counted_as_they_are_queued(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        recorder = await record(db, market_ids, adapter=Adapter(perpetual=None))
        assert recorder.unhedged == 1


async def test_the_summary_leads_with_what_went_wrong() -> None:
    coordinator = ExecutionCoordinator(Adapter(perpetual=None))
    attempt = await coordinator.execute(signal(opportunity()))
    assert attempt is not None
    text = summarise([attempt])
    assert "UNHEDGED" in text
    assert summarise([]) == "no execution attempts"
