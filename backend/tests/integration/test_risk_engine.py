"""Risk decisions against real PostgreSQL: the constraint, and restart restoration.

The unit suite (``tests/unit/test_risk_engine.py``) covers the engine's gating
logic against a fake store. What only PostgreSQL can prove is that the
``mode, intent_id, event_type`` unique constraint actually converges a
retried evaluation on one row, and that a kill-switch decision survives a
process restart because it was durably written, not because a fake happened
to remember it.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.integration.factories import make_market
from tests.unit.test_execution_coordinator import Adapter, signal
from trading_bot.api.risk_status import risk_status
from trading_bot.api.schemas import ComponentStatus
from trading_bot.core.config import ExecutionConfig, Settings
from trading_bot.db.models import Order, RiskEvent
from trading_bot.db.models.enums import ExecutionMode, MarketType, RiskDecision, RiskEventType
from trading_bot.exchange.models import MarketRef
from trading_bot.execution.coordinator import ExecutionCoordinator
from trading_bot.execution.recorder import ExecutionRecorder
from trading_bot.risk.kill_switch import KillSwitchState
from trading_bot.risk.models import RiskEventDraft
from trading_bot.risk.store import KILL_SWITCH_ADVISORY_LOCK, RiskEventStore

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)


def session_factory(db: AsyncSession):  # type: ignore[no-untyped-def]
    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield db

    return factory


CommittedSessionFactory = Callable[[], contextlib.AbstractAsyncContextManager[AsyncSession]]


@pytest.fixture
async def committed_sessions(postgres_url: str) -> AsyncIterator[CommittedSessionFactory]:
    """Independent sessions whose context exit performs a real commit.

    Kill-switch propagation is a cross-process property. Reusing the test's
    one rollback-wrapped AsyncSession would let the observer see uncommitted
    state and would not test the production transaction boundary at all.
    """
    engine = create_async_engine(postgres_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    try:
        yield factory
    finally:
        async with maker() as session:
            await session.execute(delete(RiskEvent))
            await session.commit()
        await engine.dispose()


@pytest.fixture
async def market_ids(db: AsyncSession) -> dict[MarketRef, int]:
    spot = make_market("BTCUSDT", MarketType.SPOT)
    perp = make_market("BTCUSDT", MarketType.PERPETUAL)
    db.add_all([spot, perp])
    await db.flush()
    return {SPOT: spot.id, PERP: perp.id}


def draft(
    *, intent_id: str, event_type: RiskEventType, decision: RiskDecision, reason: str = "test"
) -> RiskEventDraft:
    return RiskEventDraft(
        occurred_at=NOW,
        event_type=event_type,
        decision=decision,
        mode=ExecutionMode.PAPER,
        intent_id=intent_id,
        reason=reason,
    )


class TestIdempotentPersistence:
    async def test_retrying_the_same_decision_converges_on_one_row(self, db: AsyncSession) -> None:
        store = RiskEventStore(session_factory(db))
        first_id = await store.persist(
            draft(
                intent_id="signal:dup",
                event_type=RiskEventType.PRE_TRADE_CHECK,
                decision=RiskDecision.APPROVED,
            )
        )
        second_id = await store.persist(
            draft(
                intent_id="signal:dup",
                event_type=RiskEventType.PRE_TRADE_CHECK,
                decision=RiskDecision.APPROVED,
                reason="retried",
            )
        )
        assert first_id == second_id
        count = (
            (
                await db.execute(
                    select(RiskEvent).where(
                        RiskEvent.mode == ExecutionMode.PAPER, RiskEvent.intent_id == "signal:dup"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(count) == 1
        assert count[0].reason == "retried"

    async def test_different_event_types_for_the_same_intent_both_persist(
        self, db: AsyncSession
    ) -> None:
        store = RiskEventStore(session_factory(db))
        pre_id = await store.persist(
            draft(
                intent_id="attempt:1",
                event_type=RiskEventType.PRE_TRADE_CHECK,
                decision=RiskDecision.APPROVED,
            )
        )
        post_id = await store.persist(
            draft(
                intent_id="attempt:1",
                event_type=RiskEventType.ABNORMAL_EXECUTION,
                decision=RiskDecision.PAUSED,
            )
        )
        assert pre_id != post_id


class TestKillSwitchRestartRestoration:
    async def test_a_triggered_switch_survives_a_new_process(
        self, committed_sessions: CommittedSessionFactory
    ) -> None:
        store = RiskEventStore(committed_sessions)
        first_process = KillSwitchState(
            store, committed_sessions, mode=ExecutionMode.PAPER, clock=lambda: NOW
        )
        await first_process.load()
        assert not first_process.is_active
        await first_process.trigger(who="operator", reason="observed anomaly")

        # A fresh instance, as a restarted process would construct: nothing
        # in memory carries over, only what was written survives.
        second_process = KillSwitchState(
            store, committed_sessions, mode=ExecutionMode.PAPER, clock=lambda: NOW
        )
        await second_process.load()
        assert second_process.is_active
        assert second_process.blocked_reason() == "observed anomaly"

    async def test_a_rearmed_switch_also_survives_a_restart(
        self, committed_sessions: CommittedSessionFactory
    ) -> None:
        store = RiskEventStore(committed_sessions)
        switch = KillSwitchState(
            store, committed_sessions, mode=ExecutionMode.PAPER, clock=lambda: NOW
        )
        await switch.load()
        await switch.trigger(who="operator", reason="halt")
        await switch.rearm(who="operator", reason="reviewed, resuming")

        restarted = KillSwitchState(
            store, committed_sessions, mode=ExecutionMode.PAPER, clock=lambda: NOW
        )
        await restarted.load()
        assert not restarted.is_active


class TestCrossProcessPropagation:
    """Defect 1: a kill written by one process must reach another.

    Each side gets its *own* ``RiskEventStore`` and ``KillSwitchState``, the
    way the CLI and a running service have separate objects over one
    database. Nothing is shared in memory, so anything the observer learns it
    learned from the durable row.
    """

    async def test_a_kill_from_another_process_is_observed_on_refresh(
        self, committed_sessions: CommittedSessionFactory
    ) -> None:
        operator = KillSwitchState(
            RiskEventStore(committed_sessions),
            committed_sessions,
            mode=ExecutionMode.PAPER,
            clock=lambda: NOW,
        )
        service = KillSwitchState(
            RiskEventStore(committed_sessions),
            committed_sessions,
            mode=ExecutionMode.PAPER,
            clock=lambda: NOW,
        )
        await operator.load()
        await service.load()
        assert not service.is_active
        await operator.trigger(who="operator", reason="pulled from the CLI")
        assert service.is_active is False, "not until it looks"

        await service.refresh()

        assert service.is_active
        assert service.blocked_reason() == "pulled from the CLI"

    async def test_a_rearm_from_another_process_is_observed_too(
        self, committed_sessions: CommittedSessionFactory
    ) -> None:
        operator = KillSwitchState(
            RiskEventStore(committed_sessions),
            committed_sessions,
            mode=ExecutionMode.PAPER,
            clock=lambda: NOW,
        )
        service = KillSwitchState(
            RiskEventStore(committed_sessions),
            committed_sessions,
            mode=ExecutionMode.PAPER,
            clock=lambda: NOW,
        )
        await operator.load()
        await operator.trigger(who="operator", reason="halted")
        await service.load()
        assert service.is_active

        await operator.rearm(who="operator", reason="reviewed")
        await service.refresh()

        assert not service.is_active

    async def test_a_halt_purges_a_dispatchers_queued_work(
        self, committed_sessions: CommittedSessionFactory
    ) -> None:
        """The observation is wired to something: queued work is dropped."""
        service = KillSwitchState(
            RiskEventStore(committed_sessions),
            committed_sessions,
            mode=ExecutionMode.PAPER,
            clock=lambda: NOW,
        )
        await service.load()
        purged: list[str] = []
        service.add_listener(purged.append)

        operator = KillSwitchState(
            RiskEventStore(committed_sessions),
            committed_sessions,
            mode=ExecutionMode.PAPER,
            clock=lambda: NOW,
        )
        await operator.load()
        await operator.trigger(who="operator", reason="drop everything")
        await service.refresh()

        assert purged == ["drop everything"]


class TestSerializedKillSwitchTransitions:
    async def test_writer_waits_for_the_transaction_scoped_database_lock(
        self, committed_sessions: CommittedSessionFactory
    ) -> None:
        switch = KillSwitchState(
            RiskEventStore(committed_sessions),
            committed_sessions,
            mode=ExecutionMode.PAPER,
            clock=lambda: NOW,
        )
        await switch.load()

        async with committed_sessions() as blocker:
            await blocker.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": KILL_SWITCH_ADVISORY_LOCK},
            )
            writer = asyncio.create_task(
                switch.trigger(who="operator", reason="serialized transition")
            )
            # The independent writer cannot insert until this transaction
            # exits and releases the lock. Shield keeps it alive after the
            # timeout so it can finish below.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(writer), timeout=0.05)

        verdict = await asyncio.wait_for(writer, timeout=1)
        assert verdict.is_durable
        assert switch.is_active


class TestClockSkewCannotDecideWhatIsCurrent:
    async def test_the_newest_row_is_the_newest_id_not_the_newest_clock(
        self, committed_sessions: CommittedSessionFactory
    ) -> None:
        """Defect 14: a process with a fast clock must not resurrect a halt.

        The halt is written first but stamped an hour into the future; the
        re-arm that follows it is stamped an hour in the past. Ordering by
        ``occurred_at`` would leave this system halted forever on the strength
        of one machine's wrong clock.
        """
        store = RiskEventStore(committed_sessions)
        await store.persist(
            RiskEventDraft(
                occurred_at=NOW + timedelta(hours=1),
                event_type=RiskEventType.KILL_SWITCH,
                decision=RiskDecision.PAUSED,
                mode=ExecutionMode.PAPER,
                intent_id="kill_switch:skewed-halt",
                reason="halt from a machine whose clock runs fast",
            )
        )
        await store.persist(
            RiskEventDraft(
                occurred_at=NOW - timedelta(hours=1),
                event_type=RiskEventType.KILL_SWITCH,
                decision=RiskDecision.APPROVED,
                mode=ExecutionMode.PAPER,
                intent_id="kill_switch:skewed-rearm",
                reason="re-armed afterwards from a machine whose clock runs slow",
            )
        )

        switch = KillSwitchState(
            store, committed_sessions, mode=ExecutionMode.PAPER, clock=lambda: NOW
        )
        await switch.load()

        assert not switch.is_active, "the re-arm was written last, whatever the clocks said"


class TestRiskStatusEndpoint:
    """Defect 10: the panel reports what the engine did, not a phase number."""

    async def test_a_halted_switch_is_reported_as_degraded_with_its_reason(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "trading_bot.api.risk_status.get_session_factory", lambda: session_factory(db)
        )
        store = RiskEventStore(session_factory(db))
        switch = KillSwitchState(
            store, session_factory(db), mode=ExecutionMode.PAPER, clock=lambda: NOW
        )
        await switch.load()
        await switch.trigger(who="operator", reason="manual halt for review")

        health = await risk_status(Settings(), NOW)

        assert health.name == "Risk Engine"
        assert health.status is ComponentStatus.DEGRADED
        assert "manual halt for review" in health.detail

    async def test_decisions_it_wrote_make_it_healthy(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "trading_bot.api.risk_status.get_session_factory", lambda: session_factory(db)
        )
        store = RiskEventStore(session_factory(db))
        await store.persist(
            draft(
                intent_id="signal:healthy",
                event_type=RiskEventType.PRE_TRADE_CHECK,
                decision=RiskDecision.APPROVED,
            )
        )
        settings = Settings(execution=ExecutionConfig(shadow=True))

        health = await risk_status(settings, NOW)

        assert health.status is ComponentStatus.HEALTHY
        assert "1 approved" in health.detail

    async def test_it_is_offline_with_execution_rather_than_unbuilt(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "trading_bot.api.risk_status.get_session_factory", lambda: session_factory(db)
        )
        health = await risk_status(Settings(), NOW)

        assert health.status is ComponentStatus.OFFLINE
        assert "switched off" in health.detail
        assert "Phase" not in health.detail


class TestOrderLinkage:
    async def test_an_approved_orders_risk_event_id_is_queryable_from_the_order_row(
        self, db: AsyncSession, market_ids: dict[MarketRef, int]
    ) -> None:
        store = RiskEventStore(session_factory(db))
        risk_event_id = await store.persist(
            draft(
                intent_id="attempt:linked",
                event_type=RiskEventType.PRE_TRADE_CHECK,
                decision=RiskDecision.APPROVED,
            )
        )
        assert risk_event_id is not None

        adapter = Adapter()
        coordinator = ExecutionCoordinator(adapter)
        attempt = await coordinator.execute(
            signal(), execution_intent_id="attempt:linked", risk_event_id=risk_event_id
        )
        assert attempt is not None and attempt.is_hedged

        recorder = ExecutionRecorder(market_ids, session_factory(db), interval_seconds=1)
        recorder.record(attempt, uuid.uuid4())
        await recorder.flush()

        orders = (
            (await db.execute(select(Order).where(Order.execution_intent_id == "attempt:linked")))
            .scalars()
            .all()
        )
        assert len(orders) == 2
        assert all(order.risk_event_id == risk_event_id for order in orders)
