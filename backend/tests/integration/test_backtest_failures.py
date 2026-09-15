"""A replay that could not do or record what it claims fails - it never publishes a result.

Failure injection against the real pipeline and PostgreSQL: exceptions inside
execution, exit sweeps and feed reads; write, commit and risk-decision
failures; a ledger that diverges from its rows; and every lifecycle race the
run row can meet.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.backtest_support import (
    PAIR,
    START,
    backtest_settings,
    cleanup,
    normalized_results,
    run_async,
    seed,
)
from tests.integration.test_backtest_engine import converging_basis
from trading_bot.backtest.engine import BacktestEngine
from trading_bot.backtest.integrity import ReplayIntegrity
from trading_bot.backtest.loop import guard_sessions
from trading_bot.backtest.market_state import ReplayMarketData
from trading_bot.backtest.runs import RunIdentity, RunProgress, RunStore
from trading_bot.backtest.service import run_backtest
from trading_bot.db.models import BacktestRun, Order
from trading_bot.db.models.enums import BacktestRunStatus
from trading_bot.db.session import session_scope
from trading_bot.execution.account import PaperAccount
from trading_bot.execution.recorder import ExecutionRecorder
from trading_bot.opportunities.episodes import EpisodeTracker
from trading_bot.opportunities.recorder import OpportunityRecorder
from trading_bot.portfolio.closer import PositionCloser
from trading_bot.risk.engine import RiskEngine
from trading_bot.risk.store import RiskEventStore

pytestmark = pytest.mark.requires_postgres


@pytest.fixture(autouse=True)
def _clean(postgres_url: str) -> Iterator[None]:
    cleanup()
    yield
    cleanup()


def replay(settings: Any = None, *, seconds: int = 170, **kwargs: Any) -> Any:
    return run_backtest(
        settings or backtest_settings(),
        start=START,
        end=START + timedelta(seconds=seconds),
        refs=PAIR,
        **kwargs,
    )


def row(outcome: Any) -> BacktestRun:
    async def read(session: AsyncSession) -> BacktestRun:
        return (
            await session.execute(select(BacktestRun).where(BacktestRun.id == outcome.run.id))
        ).scalar_one()

    return run_async(read)


def rows_of(model: Any, run_id: int) -> list[Any]:
    async def read(session: AsyncSession) -> list[Any]:
        return list(
            (await session.execute(select(model).where(model.backtest_run_id == run_id))).scalars()
        )

    return run_async(read)


def assert_failed(outcome: Any, reason_prefix: str) -> BacktestRun:
    assert outcome.status is BacktestRunStatus.FAILED, outcome.failure_reason
    stored = row(outcome)
    assert stored.status is BacktestRunStatus.FAILED
    assert (stored.failure_reason or "").startswith(reason_prefix), stored.failure_reason
    assert stored.dataset_fingerprint is None
    assert stored.completeness is not None and not stored.completeness["performance_rankable"]
    assert "dataset" not in stored.completeness, "a failed run carries no verdict"
    return stored


class TestExceptionsFailTheRun:
    def test_an_exception_inside_inline_execution_fails_the_run_with_its_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(converging_basis())

        async def broken(*_: Any, **__: Any) -> None:
            raise RuntimeError("post-trade review broke")

        marked: list[Any] = []
        monkeypatch.setattr(RiskEngine, "evaluate_post_trade", broken)
        monkeypatch.setattr(
            EpisodeTracker, "mark_executed", lambda self, key, signal: marked.append(key)
        )
        outcome = replay()
        assert_failed(outcome, "RuntimeError: post-trade review broke")
        assert marked == [], "an attempt that raised is never marked executed"

    def test_a_simulator_exception_is_not_recorded_as_an_ordinary_failed_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(converging_basis())

        def broken(*_: Any, **__: Any) -> None:
            raise ArithmeticError("fill arithmetic broke")

        monkeypatch.setattr("trading_bot.execution.paper.PaperExecutionAdapter._take", broken)
        outcome = replay()
        assert_failed(outcome, "ArithmeticError: fill arithmetic broke")
        assert rows_of(Order, outcome.run.id) == []

    def test_an_exit_sweep_that_swallows_a_failure_still_fails_the_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(converging_basis())

        async def broken(*_: Any, **__: Any) -> None:
            raise RuntimeError("exit pricing broke")

        monkeypatch.setattr(PositionCloser, "close_if_due", broken)
        outcome = replay()
        stored = assert_failed(outcome, "ReplayIntegrityError: exit sweep failures changed")
        assert "exit pricing broke" in (stored.failure_reason or "")

    def test_a_lookahead_refusal_swallowed_by_the_simulator_still_fails_the_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(converging_basis(seconds=20))
        original = ReplayMarketData.covers

        def short_horizon(self: ReplayMarketData, moment: datetime) -> bool:
            # Pretend the buffer ends exactly at the decision, as a too-short
            # lookahead would; the simulator turns the refusal into "no book".
            if moment > START and moment < START + timedelta(seconds=1):
                return False
            return original(self, moment)

        monkeypatch.setattr(ReplayMarketData, "covers", short_horizon)
        monkeypatch.setattr(
            "trading_bot.backtest.feeder.Feeder.ensure_covers", _no_pull, raising=True
        )
        outcome = replay(seconds=10)
        # The simulator's task re-raises the refusal, or - where a reader
        # swallowed it - the recorded violation fails the run instead.
        stored = assert_failed(outcome, "")
        assert "lookahead horizon is too short" in (stored.failure_reason or "")


async def _no_pull(self: Any, moment: datetime) -> None:
    async with self._lock:
        while not self._market.covers(moment):
            batch = await anext(self._batches, None)
            if batch is None:
                self._market.mark_exhausted()
                return
            self._market.extend(e for e in batch.events if self._validator.accept(e))
            if moment < START + timedelta(seconds=1):
                return


class TestStrictPersistence:
    def test_a_transient_write_failure_is_retried_and_converges_on_identical_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(converging_basis())
        clean = replay()
        original = ExecutionRecorder._write
        failures = {"left": 2}

        async def flaky(self: ExecutionRecorder, *args: Any) -> int:
            if failures["left"]:
                failures["left"] -= 1
                raise ConnectionError("connection reset")
            return await original(self, *args)

        monkeypatch.setattr(ExecutionRecorder, "_write", flaky)
        retried = replay()
        assert failures["left"] == 0
        assert retried.status is BacktestRunStatus.COMPLETED, retried.failure_reason
        assert normalized_results(retried.run.id) == normalized_results(clean.run.id)
        assert retried.fingerprint == clean.fingerprint

    def test_a_lost_commit_acknowledgement_is_retried_without_duplicating_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(converging_basis())
        clean = replay()
        original_write = ExecutionRecorder._write
        original_commit = AsyncSession.commit
        armed = {"commit": False, "fired": 0}

        async def arm(self: ExecutionRecorder, *args: Any) -> int:
            written = await original_write(self, *args)
            armed["commit"] = armed["fired"] == 0
            return written

        async def commit_then_lose_the_ack(self: AsyncSession) -> None:
            await original_commit(self)
            if armed["commit"]:
                armed["commit"] = False
                armed["fired"] += 1
                raise ConnectionError("acknowledgement lost after commit")

        monkeypatch.setattr(ExecutionRecorder, "_write", arm)
        monkeypatch.setattr(AsyncSession, "commit", commit_then_lose_the_ack)
        retried = replay()
        assert armed["fired"] == 1
        assert retried.status is BacktestRunStatus.COMPLETED, retried.failure_reason
        assert normalized_results(retried.run.id) == normalized_results(clean.run.id)

    def test_a_write_that_keeps_failing_fails_the_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(converging_basis())

        async def down(*_: Any, **__: Any) -> int:
            raise ConnectionError("database down")

        monkeypatch.setattr(ExecutionRecorder, "_write", down)
        outcome = replay()
        assert_failed(
            outcome,
            "ReplayIntegrityError: orders, fills and positions: 1 record(s) still unwritten "
            "after 3 flush attempt(s): ConnectionError: database down",
        )

    def test_a_failed_final_flush_fails_the_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed(converging_basis(seconds=60))

        async def down(*_: Any, **__: Any) -> int:
            raise ConnectionError("database down")

        # Opportunities flush every 60 s of virtual time, so in a 50 s run
        # the only flush with anything to write is the final one.
        monkeypatch.setattr(OpportunityRecorder, "_write", down)
        outcome = replay(seconds=50)
        assert_failed(outcome, "ReplayIntegrityError: opportunities and signals:")

    def test_a_risk_decision_that_could_not_be_stored_fails_the_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-closed live is a different decision in replay: the run cannot stand."""
        seed(converging_basis())
        original = RiskEventStore._upsert
        calls = {"n": 0}

        async def once(self: RiskEventStore, session: AsyncSession, draft: Any) -> int:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("risk store unreachable")
            return await original(self, session, draft)

        monkeypatch.setattr(RiskEventStore, "_upsert", once)
        outcome = replay()
        stored = assert_failed(
            outcome, "ReplayIntegrityError: risk decisions that could not be stored changed"
        )
        assert "risk store unreachable" in (stored.failure_reason or "")

    def test_a_ledger_that_diverges_from_its_durable_rows_fails_the_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(converging_basis())
        monkeypatch.setattr(PaperAccount, "_pay_fee", lambda self, fee: None)
        outcome = replay(backtest_settings(costs={"spot_taker_fee_bps": 1.0}), seconds=170)
        stored = assert_failed(outcome, "ReplayIntegrityError: the paper account diverged")
        assert "cash: account" in (stored.failure_reason or "")


class _Queue:
    def __init__(self, *, fail: int) -> None:
        self.fail, self.pending, self.last_error, self.flushes = fail, 1, None, 0

    async def flush(self) -> int:
        self.flushes += 1
        if self.fail:
            self.fail -= 1
            self.last_error = "ConnectionError: nope"
            return 0
        self.pending = 0
        return 1


class TestIntegrityRules:
    def test_flush_retries_a_bounded_number_of_times(self) -> None:
        async def work() -> tuple[int, int]:
            recovered, stuck = _Queue(fail=2), _Queue(fail=5)
            await ReplayIntegrity(probes=[], queues={"q": recovered}, attempts=3).flush()
            with pytest.raises(RuntimeError, match="q: 1 record"):
                await ReplayIntegrity(probes=[], queues={"q": stuck}, attempts=3).flush()
            return recovered.flushes, stuck.flushes

        assert asyncio.run(work()) == (3, 3)

    def test_a_dropped_record_is_fatal_at_once(self) -> None:
        from trading_bot.backtest.integrity import Probe

        counter = {"dropped": 0}
        integrity = ReplayIntegrity(
            probes=[Probe("records dropped", lambda: counter["dropped"])], queues={}, attempts=3
        )
        integrity.check()
        counter["dropped"] = 1
        with pytest.raises(RuntimeError, match="records dropped changed from 0 to 1"):
            integrity.check()

    def test_the_live_recorders_count_what_their_bounded_queues_drop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("trading_bot.opportunities.recorder.MAX_PENDING_EPISODES", 1)
        recorder = OpportunityRecorder({}, session_scope, interval_seconds=1)

        class Episode:
            def __init__(self, uid: int) -> None:
                self.uid, self.duration_ms, self.strategy = uid, 10, "s"

        recorder.record([Episode(1), Episode(2)])  # type: ignore[list-item]
        assert recorder.dropped == 1 and recorder.pending == 1


class TestLifecycleRaces:
    def test_a_cancel_before_the_start_means_the_replay_never_runs(self) -> None:
        seed(converging_basis())

        async def cancel_first(run: RunIdentity, _engine: BacktestEngine) -> None:
            await RunStore(guard_sessions(session_scope)).request_cancel(run.run_uid)

        outcome = replay(on_created=cancel_first)
        assert outcome.status is BacktestRunStatus.CANCELLED
        stored = row(outcome)
        assert stored.status is BacktestRunStatus.CANCELLED
        assert stored.evaluations == 0 and stored.cancel_requested_at is not None
        assert rows_of(Order, outcome.run.id) == []

    def test_a_cancel_after_the_replay_reached_its_end_keeps_the_finished_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(converging_basis())
        original = BacktestEngine._completeness
        uid: dict[str, Any] = {}

        async def late_cancel(self: BacktestEngine, *args: Any) -> Any:
            await RunStore(guard_sessions(session_scope)).request_cancel(uid["run"])
            return await original(self, *args)

        def remember(run: RunIdentity, _engine: BacktestEngine) -> None:
            uid["run"] = run.run_uid

        monkeypatch.setattr(BacktestEngine, "_completeness", late_cancel)
        outcome = replay(on_created=remember)
        assert outcome.status is BacktestRunStatus.COMPLETED
        stored = row(outcome)
        assert stored.status is BacktestRunStatus.COMPLETED
        assert stored.cancel_requested_at is not None, "the request is kept as evidence"

    def test_a_run_recovered_as_an_orphan_stops_and_its_row_is_not_overwritten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(converging_basis(seconds=900, wide_for=0))
        original = RunStore.mark_running

        async def started_then_recovered(self: RunStore, run_id: int) -> bool:
            started = await original(self, run_id)
            async with self._session_factory() as session:
                await session.execute(
                    update(BacktestRun)
                    .where(BacktestRun.id == run_id)
                    .values(
                        status=BacktestRunStatus.FAILED,
                        completed_at=datetime.now(UTC),
                        failure_reason="interrupted: recovered by another process",
                    )
                )
            return started

        monkeypatch.setattr(RunStore, "mark_running", started_then_recovered)
        settings = backtest_settings(backtest={"heartbeat_seconds": 0.001})
        outcome = replay(settings, seconds=900)
        assert outcome.status is BacktestRunStatus.FAILED
        stored = row(outcome)
        assert stored.failure_reason == "interrupted: recovered by another process"
        assert stored.evaluations < 900

    def test_finishing_twice_never_overwrites_the_first_terminal_state(self) -> None:
        seed(converging_basis(seconds=5))
        outcome = replay(seconds=3)

        async def finish_again() -> BacktestRunStatus:
            store = RunStore(guard_sessions(session_scope))
            from trading_bot.db.session import dispose_engine, init_engine

            init_engine(backtest_settings().database)
            try:
                return await store.finish(
                    outcome.run.id,
                    BacktestRunStatus.FAILED,
                    progress=RunProgress(),
                    actual_start=None,
                    actual_end=None,
                    fingerprint=None,
                    warnings=[],
                    dataset_issues=None,
                    completeness=None,
                    failure_reason="late",
                )
            finally:
                await dispose_engine()

        assert asyncio.run(finish_again()) is outcome.status
        assert row(outcome).failure_reason is None

    def test_a_slow_finish_keeps_heart_beating(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed(converging_basis(seconds=5))
        original = BacktestEngine._completeness
        beats: list[datetime | None] = []

        async def slow(self: BacktestEngine, run: RunIdentity, *args: Any) -> Any:
            async def heartbeat_at() -> datetime | None:
                async with guard_sessions(session_scope)() as session:
                    return await session.scalar(
                        select(BacktestRun.heartbeat_at).where(BacktestRun.id == run.id)
                    )

            beats.append(await heartbeat_at())
            await asyncio.sleep(0.3)  # real time, as a slow final flush would take
            beats.append(await heartbeat_at())
            return await original(self, run, *args)

        monkeypatch.setattr(BacktestEngine, "_completeness", slow)
        outcome = replay(backtest_settings(backtest={"heartbeat_seconds": 0.02}), seconds=3)
        assert outcome.status is BacktestRunStatus.COMPLETED, outcome.failure_reason
        assert beats[0] is not None and beats[1] is not None and beats[1] > beats[0]
