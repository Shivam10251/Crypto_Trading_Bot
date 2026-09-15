"""Durability of risk decisions, and how the kill switch observes them.

The failure injection here is the point. Both of these components are only
interesting when the database misbehaves: a flush whose transaction rolls
back must lose nothing and claim nothing, and a kill switch must never
conclude "not halted" from a read it could not make.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from trading_bot.api.risk_status import kill_switch_decision_is_active
from trading_bot.db.models.enums import ExecutionMode, RiskDecision, RiskEventType
from trading_bot.risk.kill_switch import KillSwitchState
from trading_bot.risk.models import RiskEventDraft
from trading_bot.risk.store import RiskEventStore

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def draft(
    intent_id: str, *, event_type: RiskEventType = RiskEventType.PRE_TRADE_CHECK
) -> RiskEventDraft:
    return RiskEventDraft(
        occurred_at=NOW,
        event_type=event_type,
        decision=RiskDecision.REJECTED,
        mode=ExecutionMode.PAPER,
        intent_id=intent_id,
        reason="test",
    )


class _Result:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _Session:
    """Enough of ``AsyncSession`` for the store, with injectable failures."""

    def __init__(self, *, fail_on_execute_after: int | None = None) -> None:
        self.executed = 0
        self._fail_after = fail_on_execute_after

    async def execute(self, _statement: Any, *_args: Any, **_kwargs: Any) -> _Result:
        if self._fail_after is not None and self.executed >= self._fail_after:
            raise RuntimeError("connection lost mid-batch")
        self.executed += 1
        return _Result(self.executed)


def factory(
    *, fail_on_execute_after: int | None = None, fail_on_commit: bool = False
) -> tuple[Callable[[], Any], list[_Session]]:
    """A ``session_scope`` lookalike: commits on exit, rolls back on error."""
    sessions: list[_Session] = []

    @contextlib.asynccontextmanager
    async def make() -> AsyncIterator[_Session]:
        session = _Session(fail_on_execute_after=fail_on_execute_after)
        sessions.append(session)
        yield session
        if fail_on_commit:
            # The uncertain commit: every statement returned, and then the
            # transaction did not land.
            raise RuntimeError("commit failed after the statements returned")

    return make, sessions


class TestFlushIsAllOrNothing:
    async def test_a_mid_batch_failure_requeues_the_whole_batch(self) -> None:
        """Rows executed before the failure rolled back with the rest."""
        make, _sessions = factory(fail_on_execute_after=2)
        store = RiskEventStore(make)
        for index in range(5):
            store.queue(draft(f"signal:{index}"))

        written = await store.flush()

        assert written == 0
        assert store.queued_written == 0, "nothing committed, so nothing may be counted"
        assert store.pending == 5, "the two that executed rolled back too - requeue all five"
        assert store.flush_failures == 1

    async def test_a_failed_commit_requeues_everything_it_executed(self) -> None:
        make, sessions = factory(fail_on_commit=True)
        store = RiskEventStore(make)
        store.queue(draft("signal:a"))
        store.queue(draft("signal:b"))

        assert await store.flush() == 0
        assert sessions[0].executed == 2, "both statements ran..."
        assert store.pending == 2, "...and both were rolled back by the failed commit"
        assert store.queued_written == 0

    async def test_a_successful_flush_counts_and_clears(self) -> None:
        make, _sessions = factory()
        store = RiskEventStore(make)
        store.queue(draft("signal:a"))
        store.queue(draft("signal:b"))

        assert await store.flush() == 2
        assert store.queued_written == 2
        assert store.pending == 0

    async def test_a_retry_after_a_failure_writes_the_same_rows(self) -> None:
        make_failing, _ = factory(fail_on_execute_after=0)
        store = RiskEventStore(make_failing)
        store.queue(draft("signal:a"))
        await store.flush()
        assert store.pending == 1

        make_ok, _ = factory()
        store._session_factory = make_ok  # type: ignore[assignment]
        assert await store.flush() == 1
        assert store.queued_written == 1


class TestPersistCountsOnlyCommittedRows:
    async def test_a_failed_commit_is_not_counted_as_persisted(self) -> None:
        make, _sessions = factory(fail_on_commit=True)
        store = RiskEventStore(make)

        assert await store.persist(draft("signal:a")) is None
        assert store.persisted == 0, "a row whose commit failed was never persisted"
        assert store.persist_failures == 1

    async def test_a_successful_persist_is_counted_once(self) -> None:
        make, _sessions = factory()
        store = RiskEventStore(make)

        assert await store.persist(draft("signal:a")) == 1
        assert store.persisted == 1


class _SwitchRows:
    """A durable kill-switch row that tests can change between reads."""

    def __init__(self) -> None:
        self.row: dict[str, Any] | None = None
        self.reads = 0
        self.fail = False

    async def read(
        self, _factory: Any, _mode: ExecutionMode, _run: int | None = None
    ) -> dict[str, Any] | None:
        self.reads += 1
        if self.fail:
            raise RuntimeError("database unreachable")
        return self.row

    def halt(self, *, row_id: int, reason: str, halted_until: datetime | None = None) -> None:
        context: dict[str, Any] = {"action": "triggered"}
        if halted_until is not None:
            context["halted_until"] = halted_until.isoformat()
        self.row = {
            "id": row_id,
            "decision": RiskDecision.PAUSED,
            "reason": reason,
            "context": context,
            "occurred_at": NOW,
        }

    def rearm(self, *, row_id: int) -> None:
        self.row = {
            "id": row_id,
            "decision": RiskDecision.APPROVED,
            "reason": "re-armed",
            "context": {"action": "rearmed"},
            "occurred_at": NOW,
        }


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> _SwitchRows:
    source = _SwitchRows()
    monkeypatch.setattr("trading_bot.risk.kill_switch.latest_kill_switch_row", source.read)
    return source


async def switch(
    rows: _SwitchRows, *, clock: Callable[[], datetime] | None = None
) -> KillSwitchState:
    make, _sessions = factory()
    state = KillSwitchState(
        RiskEventStore(make), make, mode=ExecutionMode.PAPER, clock=clock or (lambda: NOW)
    )
    await state.load()
    return state


class TestObservingAnotherProcess:
    async def test_a_kill_written_elsewhere_is_observed_on_refresh(self, rows: _SwitchRows) -> None:
        """The CLI writes the row; a running service sees it on its next poll."""
        state = await switch(rows)
        assert not state.is_active

        rows.halt(row_id=1, reason="operator pulled the switch")
        await state.refresh()

        assert state.is_active
        assert state.blocked_reason() == "operator pulled the switch"

    async def test_a_rearm_written_elsewhere_is_observed_on_refresh(
        self, rows: _SwitchRows
    ) -> None:
        rows.halt(row_id=1, reason="halted")
        state = await switch(rows)
        assert state.is_active

        rows.rearm(row_id=2)
        await state.refresh()

        assert not state.is_active

    async def test_activation_notifies_listeners_exactly_once(self, rows: _SwitchRows) -> None:
        state = await switch(rows)
        seen: list[str] = []
        state.add_listener(seen.append)

        rows.halt(row_id=1, reason="halted")
        await state.refresh()
        await state.refresh()  # unchanged row: no second notification

        assert seen == ["halted"]

    async def test_a_failed_refresh_keeps_the_last_known_state(self, rows: _SwitchRows) -> None:
        rows.halt(row_id=1, reason="halted")
        state = await switch(rows)
        assert state.is_active

        rows.fail = True
        await state.refresh()

        assert state.is_active, "a read that failed is not evidence trading is safe"
        assert state.refresh_failures == 1

    async def test_an_unloadable_switch_starts_halted(self, rows: _SwitchRows) -> None:
        rows.fail = True
        state = await switch(rows)
        assert state.is_active
        assert state.blocked_reason() is not None

    async def test_an_unpersisted_local_halt_survives_a_refresh(self, rows: _SwitchRows) -> None:
        """The database has no record of it, which is not permission to resume."""
        make, _sessions = factory(fail_on_commit=True)
        state = KillSwitchState(
            RiskEventStore(make), make, mode=ExecutionMode.PAPER, clock=lambda: NOW
        )
        await state.load()
        verdict = await state.trigger(who="test", reason="halt now")

        assert verdict.risk_event_id is None, "the audit write failed"
        assert state.is_active
        await state.refresh()  # durable state still says nothing was ever halted
        assert state.is_active

    async def test_poll_cannot_clear_a_local_halt_while_its_write_is_in_flight(
        self, rows: _SwitchRows
    ) -> None:
        """A refresh and trigger are one serialized state machine."""

        class BlockingStore(RiskEventStore):
            def __init__(self, make: Callable[[], Any]) -> None:
                super().__init__(make)
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def persist_kill_switch(self, item: RiskEventDraft) -> int | None:
                self.started.set()
                await self.release.wait()
                rows.halt(row_id=7, reason=item.reason)
                return 7

        make, _sessions = factory()
        store = BlockingStore(make)
        state = KillSwitchState(store, make, mode=ExecutionMode.PAPER, clock=lambda: NOW)
        await state.load()

        trigger = asyncio.create_task(state.trigger(who="operator", reason="stop now"))
        await store.started.wait()
        refresh = asyncio.create_task(state.refresh())
        await asyncio.sleep(0)

        assert state.is_active
        assert not refresh.done(), "polling waits for the state transition to finish"
        store.release.set()
        await trigger
        await refresh
        assert state.is_active
        assert state.blocked_reason() == "stop now"


class TestTimedHalts:
    async def test_a_daily_loss_halt_expires_on_its_own_terms(self, rows: _SwitchRows) -> None:
        clock = NOW
        state = await switch(rows, clock=lambda: clock)
        await state.trigger(
            who="risk_engine",
            reason="daily loss limit",
            source="max_daily_loss_usd",
            halted_until=NOW + timedelta(minutes=60),
        )
        assert state.is_active

        clock = NOW + timedelta(minutes=61)
        assert not state.is_active, "the window it declared has passed"

    async def test_a_timed_halt_is_restored_with_its_window(self, rows: _SwitchRows) -> None:
        rows.halt(row_id=1, reason="daily loss", halted_until=NOW + timedelta(minutes=30))
        restarted = await switch(rows, clock=lambda: NOW + timedelta(minutes=10))
        assert restarted.is_active

        expired = await switch(rows, clock=lambda: NOW + timedelta(minutes=31))
        assert not expired.is_active

    async def test_a_pause_needing_rearm_never_expires(self, rows: _SwitchRows) -> None:
        rows.halt(row_id=1, reason="consecutive losses")
        state = await switch(rows, clock=lambda: NOW + timedelta(days=7))
        assert state.is_active

    def test_health_uses_the_same_expiry_rule_as_the_running_engine(self) -> None:
        context = {"halted_until": (NOW + timedelta(minutes=30)).isoformat()}
        assert kill_switch_decision_is_active(RiskDecision.PAUSED, context, NOW)
        assert not kill_switch_decision_is_active(
            RiskDecision.PAUSED, context, NOW + timedelta(minutes=31)
        )
