"""Opportunities and signals reaching the research record.

PostgreSQL is the point: NUMERIC precision, the CHECK constraints that refuse
a malformed opportunity, and the generated ids that signals hang off are
exactly what is under test.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.factories import make_market
from tests.unit.test_spot_perp_basis import PERP, SPOT, funding_info, view
from trading_bot.core.config import CostsConfig, SpotPerpBasisConfig
from trading_bot.db.models import (
    ExecutionMode,
    Opportunity,
    OpportunityStatus,
    Order,
    RiskEvent,
    Signal,
)
from trading_bot.db.models import Opportunity as OpportunityRow
from trading_bot.db.models.enums import (
    MarketType,
    OrderStatus,
    OrderType,
    RiskDecision,
    RiskEventType,
    Side,
    SignalStatus,
)
from trading_bot.exchange.models import MarketRef
from trading_bot.opportunities.episodes import EpisodeTracker, episode_key
from trading_bot.opportunities.recorder import OpportunityRecorder, opportunity_row
from trading_bot.strategy.base import StrategyContext
from trading_bot.strategy.basis import SpotPerpBasisStrategy
from trading_bot.strategy.costs import TransactionCostModel
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
        StrategyContext(cost_model=TransactionCostModel(costs)),
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
        """A short perpetual receives funding; the schema allows it to be signed.

        The settlement is placed inside the holding period on purpose: funding
        is discrete, so a hold that crosses none is charged nothing.
        """
        runner, clock = make_runner()
        runner.set_funding(
            {PERP: funding_info(rate="0.0001", interval=8, next_at=NOW + timedelta(minutes=10))}
        )
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
        assert row.entry_price > 0 and row.sell_entry_price is not None
        assert row.sell_entry_price > 0
        # The legacy column is no longer written: it held the sold leg's
        # ENTRY price under a name that says exit.
        assert row.exit_price is None
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
        await record(
            db,
            market_ids,
            CHEAP,
            costs=CostsConfig(spot_borrow_rate_bps_per_day=0),
        )
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

    async def test_signal_backlink_excludes_hypothetical_shadow_orders(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        runner, clock = make_runner()
        tracker = EpisodeTracker()
        evaluation = runner.evaluate(RICH)[0]
        tracker.update([evaluation], clock.now)
        item = evaluation.actionable[0]
        assert item.signal is not None
        key = episode_key(evaluation.strategy, item)
        uid = tracker.uid_for(key)
        assert uid is not None
        tracker.mark_executed(key, item.signal)

        for is_shadow in (False, True):
            db.add(
                Order(
                    market_id=market_ids[SPOT],
                    mode=ExecutionMode.PAPER,
                    opportunity_uid=uid,
                    is_shadow=is_shadow,
                    client_order_id=f"{'shadow' if is_shadow else 'signal'}-order",
                    side=Side.BUY,
                    order_type=OrderType.MARKET,
                    quantity=Decimal(1),
                    filled_quantity=Decimal(0),
                    status=OrderStatus.PENDING,
                )
            )
        await db.flush()

        recorder = OpportunityRecorder(market_ids, session_factory(db), interval_seconds=1)
        recorder.record(tracker.close_all())
        await recorder.flush()

        orders = (await db.execute(select(Order).order_by(Order.is_shadow))).scalars().all()
        real, shadow = orders
        assert real.signal_id is not None
        assert shadow.signal_id is None

    async def test_risk_decisions_are_linked_to_the_signal_they_gated(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """A risk decision is made before its signal row exists.

        It carries ``opportunity_uid`` in the meantime, exactly as an order
        does, and is linked back once the episode closes and the signals are
        written. A probe's decision is deliberately left unlinked.
        """
        runner, clock = make_runner()
        tracker = EpisodeTracker()
        evaluation = runner.evaluate(RICH)[0]
        tracker.update([evaluation], clock.now)
        item = evaluation.actionable[0]
        assert item.signal is not None
        key = episode_key(evaluation.strategy, item)
        uid = tracker.uid_for(key)
        assert uid is not None
        tracker.mark_executed(key, item.signal)

        for is_shadow in (False, True):
            db.add(
                RiskEvent(
                    occurred_at=clock.now,
                    event_type=RiskEventType.PRE_TRADE_CHECK,
                    decision=RiskDecision.APPROVED,
                    mode=ExecutionMode.PAPER,
                    intent_id=f"{'shadow' if is_shadow else 'signal'}:{uid}",
                    opportunity_uid=uid,
                    is_shadow=is_shadow,
                    reason="within every configured limit",
                )
            )
        await db.flush()

        recorder = OpportunityRecorder(market_ids, session_factory(db), interval_seconds=1)
        recorder.record(tracker.close_all())
        await recorder.flush()

        events = (await db.execute(select(RiskEvent).order_by(RiskEvent.is_shadow))).scalars().all()
        real, shadow = events
        assert real.signal_id is not None
        assert shadow.signal_id is None


class TestProvenance:
    """A row has to explain itself after retention empties the raw tables."""

    async def test_the_peak_moment_is_kept_alongside_the_opening_one(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """An episode's best moment is almost never its first.

        The row's economics describe the peak, so writing the opening time
        into the only timestamp column meant the numbers and the time on the
        same row described different instants.
        """
        runner, clock = make_runner()
        tracker = EpisodeTracker()
        recorder = OpportunityRecorder(market_ids, session_factory(db), interval_seconds=1)
        # Opens narrow, widens two seconds later, then narrows again.
        narrow = [view(SPOT, "99.99", "100.01").snapshot, view(PERP, "100.19", "100.21").snapshot]
        for snapshots in (narrow, narrow, RICH, narrow):
            recorder.record(tracker.update(runner.evaluate(snapshots), clock.now))
            clock.advance(1)
        recorder.record(tracker.close_all())
        await recorder.flush()

        row = (await db.execute(select(Opportunity))).scalars().one()
        assert row.detected_at == NOW
        assert row.best_observed_at == NOW + timedelta(seconds=2)
        assert row.last_seen_at == NOW + timedelta(seconds=3)
        assert row.samples == 4
        # ...and the stored economics are the peak's, not the opening's.
        assert row.gross_edge_bps == Decimal(100)

    async def test_the_decision_can_be_reconstructed_from_the_stored_evidence(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """Everything the strategy saw, kept where retention cannot reach it."""
        await record(db, market_ids, RICH, costs=CostsConfig())
        row = (await db.execute(select(Opportunity))).scalars().one()
        assert row.evidence is not None
        evidence = row.evidence

        episode = evidence["episode"]
        assert episode["samples"] == 3  # the default three cycles of `record`
        assert episode["best_observed_at"] == row.best_observed_at.isoformat()

        market = evidence["market"]
        assert Decimal(market["executable_quantity"]) == row.quantity
        assert Decimal(market["requested_notional_usd"]) == Decimal(1000)
        for leg in (market["buy_leg"], market["sell_leg"]):
            quote = leg["quote"]
            assert Decimal(quote["bid"]) > 0 and Decimal(quote["ask"]) > 0
            assert Decimal(quote["bid_size"]) > 0 and Decimal(quote["ask_size"]) > 0
            assert quote["local_timestamp"]
            assert leg["book"]["local_timestamp"]
            # The levels consumed on the way in and on the way back out.
            assert leg["entry_fill"]["complete"] is True
            assert leg["entry_fill"]["levels"]
            assert leg["unwind_fill"]["complete"] is True
            assert leg["unwind_fill"]["levels"]

        # The entry VWAP re-derives exactly from the levels alone.
        entry = market["buy_leg"]["entry_fill"]
        cost = sum(Decimal(price) * Decimal(size) for price, size in entry["levels"])
        assert cost / Decimal(entry["filled"]) == row.entry_price

        pricing = evidence["pricing"]
        assert pricing["cost_model_version"]
        assert pricing["assumptions"]["entry_role"] == "TAKER"
        funding = pricing["funding"]
        assert Decimal(funding["mark_price"]) > 0
        assert funding["next_funding_time"] and funding["interval_hours"] == 8
        assert funding["observed_at"] and funding["settlements"] == 0
        rates = {fee["market_type"]: fee for fee in pricing["fees"]}
        assert Decimal(rates["SPOT"]["entry_rate_bps"]) == Decimal(10)
        assert Decimal(rates["PERPETUAL"]["exit_rate_bps"]) == Decimal(5)

    async def test_no_column_calls_an_entry_price_an_exit_price(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        await record(db, market_ids, RICH)
        row = (await db.execute(select(Opportunity))).scalars().one()
        # Buy spot at the ask, sell perp at the bid: both are ENTRIES.
        assert row.entry_price == Decimal("100.01")
        assert row.sell_entry_price == Decimal("100.99")
        # The exits are the modelled unwinds, priced against the other side.
        assert row.buy_unwind_price == Decimal("99.99")
        assert row.sell_unwind_price == Decimal("101.01")
        assert row.exit_price is None

    async def test_a_signal_leg_targets_its_own_unwind_not_the_other_leg(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        await record(db, market_ids, RICH)
        signals = (await db.execute(select(Signal).order_by(Signal.side))).scalars().all()
        buy, sell = signals
        assert (buy.side, sell.side) == (Side.BUY, Side.SELL)
        # The BUY leg is closed by selling into its own bids...
        assert buy.target_entry_price == Decimal("100.01")
        assert buy.target_exit_price == Decimal("99.99")
        # ...not at the other leg's entry price, which is what used to be here.
        assert buy.target_exit_price != sell.target_entry_price

    async def test_a_legacy_row_stays_readable_and_says_it_has_no_provenance(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """Old rows are not backfilled with invented evidence.

        A row written before provenance existed keeps its values, including
        the sold leg's entry price in ``exit_price``, and is told apart from a
        new one by ``evidence IS NULL``.
        """
        legacy = Opportunity(
            uid=uuid.uuid4(),
            detected_at=NOW - timedelta(days=1),
            strategy="spot_perp_basis",
            mode=ExecutionMode.THEORETICAL,
            market_id=market_ids[SPOT],
            secondary_market_id=market_ids[PERP],
            direction=Side.BUY,
            entry_price=Decimal("100.01"),
            exit_price=Decimal("100.99"),  # the sold leg's ENTRY, under the old name
            quantity=Decimal(10),
            notional_usd=Decimal(1000),
            gross_edge_bps=Decimal(100),
            gross_edge_usd=Decimal(10),
            estimated_fees_usd=Decimal(3),
            estimated_slippage_usd=Decimal(0),
            funding_cost_usd=Decimal(0),
            borrow_cost_usd=Decimal(0),
            other_costs_usd=Decimal(0),
            safety_buffer_usd=Decimal(0),
            net_edge_bps=Decimal(70),
            net_edge_usd=Decimal(7),
            status=OpportunityStatus.REJECTED,
            rejection_reason="BELOW_MIN_EDGE",
        )
        db.add(legacy)
        await db.flush()
        await record(db, market_ids, RICH)

        rows = (await db.execute(select(Opportunity).order_by(Opportunity.detected_at))).scalars()
        old, new = rows.all()
        # Readable, unchanged, and explicitly without provenance.
        assert old.net_edge_bps == Decimal(70)
        assert old.exit_price == Decimal("100.99")
        assert old.evidence is None
        assert old.best_observed_at is None and old.samples is None
        # A query can separate the two populations on exactly that.
        assert new.evidence is not None and new.best_observed_at is not None

    async def test_research_can_tell_the_four_outcomes_apart(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """Unpriceable, priced-and-rejected and validated are three answers."""
        await record(db, market_ids, RICH)  # zero fees -> validated
        unpriceable_runner, clock = make_runner()
        unpriceable_runner.set_funding({})
        tracker = EpisodeTracker()
        recorder = OpportunityRecorder(market_ids, session_factory(db), interval_seconds=1)
        recorder.record(tracker.update(unpriceable_runner.evaluate(CHEAP), clock.now))
        recorder.record(tracker.close_all())
        await recorder.flush()
        # A 20 bps basis against 30 bps of fees: priced, and rejected.
        thin = [view(SPOT, "99.99", "100.01").snapshot, view(PERP, "100.19", "100.21").snapshot]
        await record(db, market_ids, thin, costs=CostsConfig())

        statuses = {
            row.status: row for row in (await db.execute(select(Opportunity))).scalars().all()
        }
        assert set(statuses) == {
            OpportunityStatus.VALIDATED,
            OpportunityStatus.REJECTED,
            OpportunityStatus.UNPRICEABLE,
        }
        # Only the unpriceable one has no net edge - never a fabricated zero.
        assert statuses[OpportunityStatus.UNPRICEABLE].net_edge_bps is None
        assert statuses[OpportunityStatus.REJECTED].net_edge_bps is not None
        assert statuses[OpportunityStatus.VALIDATED].net_edge_bps is not None


class TestDurability:
    async def test_an_unpriceable_episode_is_stored_without_an_invented_edge(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """It happened, so it is counted - but its costs stay unknown.

        Dropping it lost the observation entirely, which is a worse answer to
        "how many opportunities were there?" than a row saying "this one could
        not be priced". A fabricated zero would be worse than either.
        """
        runner, clock = make_runner()
        runner.set_funding({})  # no funding interval -> unpriceable
        tracker = EpisodeTracker()
        recorder = OpportunityRecorder(market_ids, session_factory(db), interval_seconds=1)
        recorder.record(tracker.update(runner.evaluate(RICH), clock.now))
        recorder.record(tracker.close_all())
        assert await recorder.flush() == 1
        assert recorder.unpriced == 1
        row = (await db.execute(select(Opportunity))).scalars().one()
        assert row.status is OpportunityStatus.UNPRICEABLE
        assert row.net_edge_bps is None and row.net_edge_usd is None
        assert row.estimated_fees_usd is None and row.funding_cost_usd is None
        assert row.rejection_reason == "FUNDING_UNKNOWN"
        # The observation itself is intact: it just has no price on it.
        assert row.gross_edge_bps > 0 and row.quantity > 0

    async def test_recording_the_same_episode_twice_writes_one_row(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """Shutdown closes episodes the flush loop may already have queued.

        The row's identity is fixed when the episode opens, so the second
        offer of the same episode is recognised as the same observation
        rather than written as a second one.
        """
        runner, clock = make_runner()
        tracker = EpisodeTracker()
        recorder = OpportunityRecorder(market_ids, session_factory(db), interval_seconds=1)
        tracker.update(runner.evaluate(RICH), clock.now)
        closed = tracker.close_all()
        recorder.record(closed)
        recorder.record(closed)  # the same episode, offered again
        assert await recorder.flush() == 1
        assert len((await db.execute(select(Opportunity))).scalars().all()) == 1

    async def test_a_retried_flush_presents_the_same_row_not_a_new_one(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        """A failed write keeps the queue; the retry must not double-count.

        The uid is the episode's, not the attempt's, so if the first attempt
        did reach the database the unique index refuses the second - rather
        than the same moment being stored as two opportunities.
        """
        runner, clock = make_runner()
        tracker = EpisodeTracker()
        tracker.update(runner.evaluate(RICH), clock.now)
        (episode,) = tracker.close_all()
        first = opportunity_row(episode, market_ids)
        second = opportunity_row(episode, market_ids)
        assert first is not None and second is not None
        assert first["uid"] == second["uid"] == episode.uid

        recorder = OpportunityRecorder(market_ids, session_factory(db), interval_seconds=1)
        recorder.record([episode])
        assert await recorder.flush() == 1
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(insert(OpportunityRow).values(second))

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
