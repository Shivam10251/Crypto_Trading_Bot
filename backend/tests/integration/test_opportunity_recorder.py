"""Opportunities and signals reaching the research record.

PostgreSQL is the point: NUMERIC precision, the CHECK constraints that refuse
a malformed opportunity, and the generated ids that signals hang off are
exactly what is under test.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.factories import make_market
from tests.unit.test_spot_perp_basis import PERP, SPOT, funding_info, view
from trading_bot.core.config import CostsConfig, SpotPerpBasisConfig
from trading_bot.db.models import ExecutionMode, Opportunity, OpportunityStatus, Signal
from trading_bot.db.models.enums import MarketType, Side, SignalStatus
from trading_bot.exchange.models import MarketRef
from trading_bot.opportunities.episodes import EpisodeTracker
from trading_bot.opportunities.recorder import OpportunityRecorder
from trading_bot.strategy.base import StrategyContext
from trading_bot.strategy.basis import SpotPerpBasisStrategy
from trading_bot.strategy.costs import ConfiguredCostModel
from trading_bot.strategy.runner import StrategyRunner

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 9, 11, 16, 30, tzinfo=UTC)
FREE = CostsConfig(spot_taker_fee_bps=0.0, perp_taker_fee_bps=0.0, safety_buffer_bps=0.0)
RICH = [view(SPOT, "99.99", "100.01").snapshot, view(PERP, "100.99", "101.01").snapshot]
CHEAP = [view(SPOT, "100.99", "101.01").snapshot, view(PERP, "99.99", "100.01").snapshot]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
async def market_ids(db: AsyncSession) -> dict[MarketRef, int]:
    spot = make_market("BTCUSDT", MarketType.SPOT)
    perp = make_market("BTCUSDT", MarketType.PERPETUAL)
    db.add_all([spot, perp])
    await db.flush()
    return {SPOT: spot.id, PERP: perp.id}


def session_factory(db: AsyncSession):  # type: ignore[no-untyped-def]
    """Hands the recorder the test's transaction, so rollback still cleans up."""

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield db

    return factory


def make_runner(costs: CostsConfig = FREE) -> tuple[StrategyRunner, Clock]:
    clock = Clock()
    runner = StrategyRunner(
        [SpotPerpBasisStrategy(SpotPerpBasisConfig())],
        StrategyContext(cost_model=ConfiguredCostModel(costs, funding_horizon=timedelta(hours=1))),
        clock=clock,
    )
    runner.set_funding({PERP: funding_info(interval=8)})
    return runner, clock


async def record(
    db: AsyncSession,
    market_ids: dict[MarketRef, int],
    snapshots: list[object],
    *,
    costs: CostsConfig = FREE,
    cycles: int = 3,
) -> OpportunityRecorder:
    runner, clock = make_runner(costs)
    tracker = EpisodeTracker()
    recorder = OpportunityRecorder(market_ids, session_factory(db), interval_seconds=1)
    for _ in range(cycles):
        recorder.record(tracker.update(runner.evaluate(snapshots), clock.now))
        clock.advance(1)
    recorder.record(tracker.close_all())
    await recorder.flush()
    return recorder


class TestOpportunityRows:
    async def test_an_episode_becomes_one_row(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """Three evaluations of the same discrepancy, one opportunity."""
        recorder = await record(db, market_ids, RICH, cycles=3)
        rows = (await db.execute(select(Opportunity))).scalars().all()
        assert len(rows) == 1
        assert recorder.opportunities_written == 1
        row = rows[0]
        assert row.strategy == "spot_perp_basis"
        assert row.mode is ExecutionMode.THEORETICAL
        assert row.duration_ms == 2000  # first observation to last

    async def test_both_legs_are_linked(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        await record(db, market_ids, RICH)
        row = (await db.execute(select(Opportunity))).scalars().one()
        assert row.market_id == market_ids[SPOT]  # the bought leg
        assert row.secondary_market_id == market_ids[PERP]  # the sold leg
        assert row.direction is Side.BUY

    async def test_costs_are_itemised_so_lost_edge_can_be_attributed(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        await record(db, market_ids, RICH, costs=CostsConfig())
        row = (await db.execute(select(Opportunity))).scalars().one()
        assert row.estimated_fees_usd > 0
        assert row.safety_buffer_usd > 0
        assert row.net_edge_usd == row.gross_edge_usd - (
            row.estimated_fees_usd
            + row.estimated_slippage_usd
            + row.funding_cost_usd
            + row.borrow_cost_usd
            + row.other_costs_usd
            + row.safety_buffer_usd
        )

    async def test_a_funding_credit_is_stored_as_a_negative_cost(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """A short perpetual receives funding; the schema allows it to be signed."""
        runner, clock = make_runner()
        runner.set_funding({PERP: funding_info(rate="0.0001", interval=8)})
        tracker = EpisodeTracker()
        recorder = OpportunityRecorder(market_ids, session_factory(db), interval_seconds=1)
        recorder.record(tracker.update(runner.evaluate(RICH), clock.now))
        recorder.record(tracker.close_all())
        await recorder.flush()
        row = (await db.execute(select(Opportunity))).scalars().one()
        assert row.funding_cost_usd < 0

    async def test_prices_are_stored_so_a_better_fee_model_can_re_cost_it(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """The reason recording early loses nothing: the inputs survive."""
        await record(db, market_ids, RICH, costs=CostsConfig())
        row = (await db.execute(select(Opportunity))).scalars().one()
        assert row.entry_price > 0 and row.exit_price > 0
        assert row.quantity > 0 and row.notional_usd > 0
        assert row.gross_edge_bps > 0
        # Re-deriving net under a zero-fee model needs nothing but the row.
        recomputed = row.gross_edge_usd - row.estimated_slippage_usd - row.funding_cost_usd
        assert recomputed > row.net_edge_usd


class TestStatusAndRejection:
    async def test_an_opportunity_that_passed_every_gate_is_validated(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        await record(db, market_ids, RICH)
        row = (await db.execute(select(Opportunity))).scalars().one()
        # VALIDATED, never EXECUTED: nothing executes before Phase 8.
        assert row.status is OpportunityStatus.VALIDATED
        assert row.rejection_reason is None

    async def test_a_rejection_is_stored_with_its_reason(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """The central research question needs the reason, not just the verdict."""
        await record(db, market_ids, CHEAP, costs=CostsConfig())
        row = (await db.execute(select(Opportunity))).scalars().one()
        assert row.status is OpportunityStatus.REJECTED
        assert row.rejection_reason is not None
        assert "SPOT_SHORT_UNAVAILABLE" in row.rejection_reason

    async def test_multiple_reasons_are_stored_in_a_groupable_order(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """Research groups by this column; "A, B" and "B, A" must not split it."""
        runner, clock = make_runner(CostsConfig())
        tracker = EpisodeTracker()
        recorder = OpportunityRecorder(market_ids, session_factory(db), interval_seconds=1)
        # A thin sell-spot basis fails the edge gate and the short gate, and
        # which one is reported first depends on the order they were hit.
        thin_cheap = [
            view(SPOT, "100.049", "100.051").snapshot,
            view(PERP, "99.999", "100.001").snapshot,
        ]
        recorder.record(tracker.update(runner.evaluate(thin_cheap), clock.now))
        clock.advance(1)
        recorder.record(tracker.update(runner.evaluate(CHEAP), clock.now))
        recorder.record(tracker.close_all())
        await recorder.flush()
        row = (await db.execute(select(Opportunity))).scalars().one()
        assert row.rejection_reason is not None
        reasons = row.rejection_reason.split(", ")
        assert reasons == sorted(reasons)

    async def test_unprofitable_opportunities_are_kept_not_discarded(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """A dataset of only the good ones cannot say how many chances existed."""
        thin = [
            view(SPOT, "99.999", "100.001").snapshot,
            view(PERP, "100.049", "100.051").snapshot,
        ]
        await record(db, market_ids, thin, costs=CostsConfig())
        row = (await db.execute(select(Opportunity))).scalars().one()
        assert row.net_edge_bps < 0
        assert row.status is OpportunityStatus.REJECTED


class TestSignals:
    async def test_a_validated_opportunity_writes_one_signal_per_leg(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        recorder = await record(db, market_ids, RICH)
        signals = (await db.execute(select(Signal))).scalars().all()
        assert len(signals) == 2
        assert recorder.signals_written == 2
        assert {s.side for s in signals} == {Side.BUY, Side.SELL}
        assert {s.market_id for s in signals} == set(market_ids.values())

    async def test_signals_attach_to_their_own_opportunity(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """A batched insert must not cross-link signals to the wrong row."""
        eth_spot = MarketRef(SPOT.venue, "ETHUSDT", SPOT.market_type)
        eth_perp = MarketRef(PERP.venue, "ETHUSDT", PERP.market_type)
        eth_s = make_market("ETHUSDT", MarketType.SPOT)
        eth_p = make_market("ETHUSDT", MarketType.PERPETUAL)
        db.add_all([eth_s, eth_p])
        await db.flush()
        ids = {**market_ids, eth_spot: eth_s.id, eth_perp: eth_p.id}

        runner, clock = make_runner()
        runner.set_funding({PERP: funding_info(interval=8), eth_perp: funding_info(interval=8)})
        tracker = EpisodeTracker()
        recorder = OpportunityRecorder(ids, session_factory(db), interval_seconds=1)
        recorder.record(
            tracker.update(
                runner.evaluate(
                    [
                        *RICH,
                        view(eth_spot, "99.99", "100.01").snapshot,
                        view(eth_perp, "102.99", "103.01").snapshot,
                    ]
                ),
                clock.now,
            )
        )
        recorder.record(tracker.close_all())
        await recorder.flush()

        rows = (await db.execute(select(Opportunity))).scalars().all()
        assert len(rows) == 2
        by_id = {row.id: row for row in rows}
        for signal in (await db.execute(select(Signal))).scalars().all():
            opportunity = by_id[signal.opportunity_id]
            # Every signal's market must be one of its own opportunity's legs.
            assert signal.market_id in (opportunity.market_id, opportunity.secondary_market_id)
            assert signal.expected_net_edge_bps == opportunity.net_edge_bps

    async def test_a_rejected_opportunity_writes_no_signal(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        await record(db, market_ids, CHEAP, costs=CostsConfig())
        assert (await db.execute(select(Signal))).scalars().all() == []

    async def test_signals_are_generated_not_executed(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        await record(db, market_ids, RICH)
        for signal in (await db.execute(select(Signal))).scalars().all():
            assert signal.status is SignalStatus.GENERATED
            assert signal.expires_at is not None


class TestDurability:
    async def test_an_unpriceable_episode_is_counted_not_invented(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """No net edge means no row - a fabricated zero would poison every query."""
        runner, clock = make_runner()
        runner.set_funding({})  # no funding interval -> unpriceable
        tracker = EpisodeTracker()
        recorder = OpportunityRecorder(market_ids, session_factory(db), interval_seconds=1)
        recorder.record(tracker.update(runner.evaluate(RICH), clock.now))
        recorder.record(tracker.close_all())
        assert await recorder.flush() == 0
        assert recorder.unpriced == 1
        assert (await db.execute(select(Opportunity))).scalars().all() == []

    async def test_shutdown_flushes_open_episodes(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """An episode still running when the process stops is not lost."""
        runner, clock = make_runner()
        tracker = EpisodeTracker()
        recorder = OpportunityRecorder(market_ids, session_factory(db), interval_seconds=1)
        tracker.update(runner.evaluate(RICH), clock.now)  # never closed normally
        assert tracker.open_episodes()
        recorder.record(tracker.close_all())
        assert await recorder.flush() == 1

    async def test_a_market_the_recorder_does_not_know_is_skipped(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        recorder = await record(db, {}, RICH)
        assert recorder.opportunities_written == 0
        assert (await db.execute(select(Opportunity))).scalars().all() == []

    async def test_short_episodes_can_be_filtered_by_configuration(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        runner, clock = make_runner()
        tracker = EpisodeTracker()
        recorder = OpportunityRecorder(
            market_ids, session_factory(db), interval_seconds=1, min_duration_ms=5000
        )
        tracker.update(runner.evaluate(RICH), clock.now)
        recorder.record(tracker.close_all())  # 0 ms, below the floor
        assert await recorder.flush() == 0
