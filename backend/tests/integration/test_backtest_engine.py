"""Replaying recorded history through the real pipeline, against PostgreSQL.

Synchronous tests on purpose: a backtest owns its event loop (virtual time
needs ``ReplayEventLoop``), so it cannot run inside pytest-asyncio's loop.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.backtest_support import (
    PAIR,
    PERP,
    SPOT,
    START,
    DatasetBuilder,
    backtest_settings,
    cleanup,
    normalized_results,
    run_async,
    seed,
    with_factory,
)
from trading_bot.backtest.engine import BacktestEngine
from trading_bot.backtest.loop import guard_sessions
from trading_bot.backtest.postgres_source import PostgresHistoricalSource
from trading_bot.backtest.report import build_report
from trading_bot.backtest.runs import RunIdentity, RunStore
from trading_bot.backtest.service import run_backtest
from trading_bot.db.models import BacktestRun, Fill, Opportunity, Order, Position, RiskEvent
from trading_bot.db.models.enums import (
    BacktestRunStatus,
    ExecutionMode,
    OpportunityStatus,
    PositionStatus,
    RiskDecision,
    RiskEventType,
)
from trading_bot.db.session import session_scope
from trading_bot.portfolio.store import PortfolioStore

pytestmark = pytest.mark.requires_postgres


@pytest.fixture(autouse=True)
def _clean(postgres_url: str) -> Iterator[None]:
    cleanup()
    yield
    cleanup()


def converging_basis(seconds: int = 180, *, wide_for: int = 20) -> DatasetBuilder:
    """A 20 bps perp premium for ``wide_for`` seconds, then fully converged."""
    dataset = DatasetBuilder()
    for second in range(seconds):
        at = START + timedelta(seconds=second)
        basis = Decimal(20) if second < wide_for else Decimal(0)
        dataset.market_pair(at, spot_mid=Decimal(100_000), basis_bps=basis)
        if second % 30 == 0:
            dataset.observe_funding(PERP, at, mark="100100")
    return dataset


def replay(settings: Any = None, *, seconds: int = 170, **kwargs: Any) -> Any:
    return run_backtest(
        settings or backtest_settings(),
        start=START,
        end=START + timedelta(seconds=seconds),
        refs=PAIR,
        **kwargs,
    )


def fetch(model: Any, *conditions: Any) -> list[Any]:
    async def read(session: AsyncSession) -> list[Any]:
        return list((await session.execute(select(model).where(*conditions))).scalars())

    return run_async(read)


class TestRoundTrip:
    def test_a_converging_basis_opens_and_closes_a_paired_trade(self) -> None:
        seed(converging_basis())
        outcome = replay()
        assert outcome.status is BacktestRunStatus.COMPLETED, outcome.failure_reason
        run_id = outcome.run.id

        orders = fetch(Order, Order.backtest_run_id == run_id)
        assert {(order.intent, order.status.value) for order in orders} == {
            ("OPEN", "FILLED"),
            ("CLOSE", "FILLED"),
        }
        assert len(orders) == 4
        assert all(order.mode is ExecutionMode.BACKTEST for order in orders)
        entries = [order for order in orders if order.intent == "OPEN"]
        # Submitted at the evaluation, filled after the configured 100 ms.
        assert {order.submitted_at for order in entries} == {START}
        fills = fetch(Fill, Fill.backtest_run_id == run_id)
        assert {fill.filled_at for fill in fills if fill.order_id in {o.id for o in entries}} == {
            START + timedelta(milliseconds=100)
        }
        positions = fetch(Position, Position.backtest_run_id == run_id)
        assert {position.status for position in positions} == {PositionStatus.CLOSED}
        assert {position.exit_reason for position in positions} == {"BASIS_CONVERGED"}
        # Realised from the fills: bought spot at 100000.5, sold at 99999.5 on
        # 0.009; sold the perp at 100199.5 and bought it back at 100000.5.
        assert sum(position.realized_pnl_usd for position in positions) == Decimal("1.782")
        # No settlement was crossed, so funding was measured - as zero.
        perp = next(
            p for p in positions if p.funding_pnl_usd is not None and p.side.value == "SELL"
        )
        assert perp.funding_pnl_usd == Decimal(0)

        report = with_factory(lambda factory: build_report(factory, outcome.run.run_uid))
        assert report.trade_count == 1
        assert report.realized_pnl_usd == Decimal("1.782")
        assert report.pnl_complete, report.unmeasured_components
        assert report.attempts == {"hedged": 1}
        # Only the two entry fills answer to a signal; sampled books cannot move in 100 ms.
        assert report.fills_on_book_not_after_signal == 2
        assert report.fills_on_book_after_signal == 0
        assert report.return_interval_seconds == 60
        assert report.status == "COMPLETED"


class TestDeterminismAndIsolation:
    def test_the_same_dataset_and_configuration_reproduce_identically_in_isolation(self) -> None:
        seed(converging_basis())
        first = replay()
        second = replay()
        assert first.run.id != second.run.id
        assert first.status is second.status is BacktestRunStatus.COMPLETED
        assert first.fingerprint == second.fingerprint
        assert first.run.config_hash == second.run.config_hash
        a, b = normalized_results(first.run.id), normalized_results(second.run.id)
        assert a["orders"], "the comparison must include executions"
        assert a == b

        # Identical client order ids, snapshot instants and intent ids now
        # coexist - which only run-keyed uniqueness allows.
        async def per_scope(factory: Any) -> tuple[int, int, int]:
            first_store = PortfolioStore(
                factory, mode=ExecutionMode.BACKTEST, backtest_run_id=first.run.id
            )
            paper_store = PortfolioStore(factory, mode=ExecutionMode.PAPER)
            end = START + timedelta(days=1)
            return (
                len(await first_store.attempts_closed_between("replaytest", None, end)),
                len(await paper_store.attempts_closed_between("replaytest", None, end)),
                len(await paper_store.live_attempts("replaytest")),
            )

        assert with_factory(per_scope) == (1, 0, 0)

    def test_a_different_configuration_is_a_different_hash_and_identity(self) -> None:
        seed(converging_basis())
        base = replay()
        changed = replay(backtest_settings(execution={"latency_ms": 150}))
        assert base.run.config_hash != changed.run.config_hash
        uids = {row[0] for row in normalized_results(base.run.id)["opportunities"]}
        other = {row[0] for row in normalized_results(changed.run.id)["opportunities"]}
        assert uids.isdisjoint(other)


def lookahead_fill_dataset() -> DatasetBuilder:
    """A signal at t=0, a worse book at +50 ms, and a spectacular one at +150 ms."""
    dataset = DatasetBuilder()
    dataset.observe_funding(PERP, START, mark="100200")
    dataset.market_pair(START, spot_mid=Decimal(100_000), basis_bps=Decimal(20))
    worse = START + timedelta(milliseconds=50)
    dataset.book(SPOT, worse, "100009.5", "100010.5")
    dataset.book(PERP, worse, "100189.5", "100190.5")
    future = START + timedelta(milliseconds=150)
    dataset.book(SPOT, future, "90000", "90001")
    dataset.book(PERP, future, "150000", "150001")
    for second in range(1, 12):
        dataset.market_pair(
            START + timedelta(seconds=second), spot_mid=Decimal(100_000), basis_bps=Decimal(0)
        )
    return dataset


class TestNoLookAhead:
    def test_a_fill_is_priced_on_the_book_at_arrival_never_a_later_one(self) -> None:
        seed(lookahead_fill_dataset())
        outcome = replay(
            backtest_settings(
                backtest={"max_book_carry_ms": 30_000}, portfolio={"exits": {"enabled": False}}
            ),
            seconds=3,
        )
        fills = fetch(Fill, Fill.backtest_run_id == outcome.run.id)
        orders = {o.id: o for o in fetch(Order, Order.backtest_run_id == outcome.run.id)}
        entry_fills = [fill for fill in fills if orders[fill.order_id].intent == "OPEN"]
        assert len(entry_fills) == 2, outcome
        by_side = {orders[fill.order_id].side.value: fill for fill in entry_fills}
        # Bought spot from the +50 ms asks, sold the perp into its +50 ms bids.
        assert by_side["BUY"].price == Decimal("100010.5")
        assert by_side["SELL"].price == Decimal("100189.5")
        arrival_book = START + timedelta(milliseconds=50)
        assert {fill.book_local_timestamp for fill in entry_fills} == {arrival_book}
        assert all(fill.price < Decimal(150_000) for fill in fills)

    def test_future_profitable_data_cannot_create_a_decision(self) -> None:
        dataset = DatasetBuilder()
        dataset.observe_funding(PERP, START, mark="100000")
        for second in range(0, 12):
            at = START + timedelta(seconds=second)
            dataset.market_pair(at, spot_mid=Decimal(100_000), basis_bps=Decimal(0))
            # A 50 bps premium one microsecond after each evaluation, gone
            # again long before the next one.
            blip = at + timedelta(microseconds=1)
            dataset.market_pair(blip, spot_mid=Decimal(100_000), basis_bps=Decimal(50))
            dataset.market_pair(
                at + timedelta(milliseconds=500), spot_mid=Decimal(100_000), basis_bps=Decimal(0)
            )
        seed(dataset)
        outcome = replay(seconds=10)
        assert outcome.status is BacktestRunStatus.COMPLETED, outcome.failure_reason
        assert fetch(Order, Order.backtest_run_id == outcome.run.id) == []
        opportunities = fetch(Opportunity, Opportunity.backtest_run_id == outcome.run.id)
        assert all(o.status is not OpportunityStatus.VALIDATED for o in opportunities)
        assert outcome.progress.evaluations == 10


class TestSignalExpiry:
    def test_a_second_signal_waiting_behind_an_execution_expires(self) -> None:
        """Replay executes one decision at a time, so the second waits a latency."""
        eth_spot = SPOT.__class__(SPOT.venue, "ETHUSDT", SPOT.market_type)
        eth_perp = PERP.__class__(PERP.venue, "ETHUSDT", PERP.market_type)
        dataset = converging_basis(seconds=5, wide_for=5)
        for second in range(5):
            at = START + timedelta(seconds=second)
            for ref, mid in ((eth_spot, Decimal(100_000)), (eth_perp, Decimal(100_200))):
                bid, ask = str(mid - Decimal("0.5")), str(mid + Decimal("0.5"))
                dataset.quote(ref, at, bid, ask, size="1")
                dataset.book(ref, at, bid, ask)
        dataset.observe_funding(eth_perp, START, mark="100200")
        seed(dataset, refs=(*PAIR, eth_spot, eth_perp))
        settings = backtest_settings(
            strategy={"spot_perp_basis": {"signal_ttl_ms": 50}},
            risk={"max_total_exposure_usd": 100_000, "max_position_notional_usd": 5_000},
        )
        outcome = run_backtest(
            settings,
            start=START,
            end=START + timedelta(seconds=1),
            refs=(*PAIR, eth_spot, eth_perp),
        )
        events = fetch(RiskEvent, RiskEvent.backtest_run_id == outcome.run.id)
        kinds = sorted(event.event_type.value for event in events)
        assert "PRE_TRADE_CHECK" in kinds
        assert "SIGNAL_EXPIRED" in kinds, kinds


class TestLifecycle:
    def test_a_durable_cancel_stops_a_running_backtest(self) -> None:
        seed(converging_basis(seconds=900, wide_for=0))
        settings = backtest_settings(
            backtest={"heartbeat_seconds": 0.001, "orphan_after_seconds": 5.0}
        )

        def cancel_soon(run: RunIdentity, _engine: BacktestEngine) -> None:
            async def request() -> None:
                await RunStore(guard_sessions(session_scope)).request_cancel(run.run_uid)

            asyncio.get_running_loop().create_task(request())

        outcome = replay(settings, seconds=900, on_created=cancel_soon)
        assert outcome.status is BacktestRunStatus.CANCELLED
        (row,) = fetch(BacktestRun, BacktestRun.id == outcome.run.id)
        assert row.status is BacktestRunStatus.CANCELLED
        assert row.completed_at is not None and row.cancel_requested_at is not None
        assert row.dataset_fingerprint is None, "a partial replay has no dataset fingerprint"
        assert row.evaluations < 900

    def test_an_interrupted_run_is_recovered_as_failed(self) -> None:
        stale = datetime.now(UTC) - timedelta(hours=1)

        async def orphan(session: AsyncSession) -> uuid.UUID:
            run = BacktestRun(
                run_uid=uuid.uuid4(),
                status=BacktestRunStatus.RUNNING,
                dataset_source="postgres",
                requested_start=START,
                requested_end=START + timedelta(minutes=1),
                markets=[],
                config_snapshot={},
                config_hash="0" * 64,
                started_at=stale,
                heartbeat_at=stale,
            )
            session.add(run)
            await session.flush()
            return run.run_uid

        uid = run_async(orphan)
        seed(converging_basis(seconds=5))
        replay(seconds=3)
        (row,) = fetch(BacktestRun, BacktestRun.run_uid == uid)
        assert row.status is BacktestRunStatus.FAILED
        assert "interrupted" in (row.failure_reason or "")

    def test_a_failure_mid_replay_is_recorded_with_its_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(converging_basis(seconds=60))
        original = PostgresHistoricalSource.stream

        async def failing(self: PostgresHistoricalSource, request: Any) -> AsyncIterator[Any]:
            async for batch in original(self, request):
                yield batch
                raise RuntimeError("storage went away")

        monkeypatch.setattr(PostgresHistoricalSource, "stream", failing)
        outcome = replay(backtest_settings(backtest={"batch_size": 100}), seconds=50)
        assert outcome.status is BacktestRunStatus.FAILED
        (row,) = fetch(BacktestRun, BacktestRun.id == outcome.run.id)
        assert row.failure_reason == "RuntimeError: storage went away"
        assert row.dataset_fingerprint is None

    def test_no_recorded_data_fails_rather_than_completing_on_nothing(self) -> None:
        seed(DatasetBuilder())
        outcome = replay(seconds=10)
        assert outcome.status is BacktestRunStatus.FAILED
        assert "no recorded events" in (outcome.failure_reason or "")

    def test_a_market_without_depth_or_funding_finishes_incomplete(self) -> None:
        dataset = DatasetBuilder()
        for second in range(30):
            at = START + timedelta(seconds=second)
            dataset.quote(SPOT, at, "99999.5", "100000.5")
            dataset.quote(PERP, at, "100199.5", "100200.5")
        seed(dataset)
        outcome = replay(seconds=20)
        assert outcome.status is BacktestRunStatus.INCOMPLETE
        missing = outcome.completeness["dataset"]["missing"]
        assert {f"{SPOT}:BOOK", f"{PERP}:BOOK", f"{PERP}:FUNDING"} <= set(missing)
        assert not outcome.completeness["performance_rankable"]
        assert fetch(Order, Order.backtest_run_id == outcome.run.id) == []
        opportunities = fetch(Opportunity, Opportunity.backtest_run_id == outcome.run.id)
        assert opportunities == [], "no synchronised book means no basis to price"


def test_risk_decisions_are_scoped_and_stamped_in_virtual_time() -> None:
    seed(converging_basis())
    outcome = replay()
    events = fetch(RiskEvent, RiskEvent.backtest_run_id == outcome.run.id)
    approvals = [e for e in events if e.decision is RiskDecision.APPROVED]
    assert approvals and all(e.mode is ExecutionMode.BACKTEST for e in events)
    assert {e.occurred_at for e in approvals if e.event_type is RiskEventType.PRE_TRADE_CHECK} == {
        START
    }
