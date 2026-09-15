"""Scheduling execution once per opportunity episode without blocking strategy."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import pytest

from tests.unit.test_execution_coordinator import Adapter, signal
from trading_bot.db.models.enums import RiskDecision
from trading_bot.execution.account import AccountRejection
from trading_bot.execution.coordinator import ExecutionAttempt, ExecutionCoordinator
from trading_bot.execution.dispatcher import ExecutionDispatcher
from trading_bot.execution.models import RejectionCode
from trading_bot.strategy.models import Signal


class Recorder:
    def __init__(self) -> None:
        self.items: list[tuple[ExecutionAttempt, uuid.UUID | None]] = []
        self.recorded = asyncio.Event()

    def record(self, attempt: ExecutionAttempt, opportunity_uid: uuid.UUID | None = None) -> None:
        self.items.append((attempt, opportunity_uid))
        self.recorded.set()


@dataclass(frozen=True, slots=True)
class _FakeVerdict:
    is_approved: bool
    risk_event_id: int | None
    decision: RiskDecision


class FakeRiskEngine:
    """The interface ``ExecutionDispatcher`` needs, without a database."""

    def __init__(
        self,
        *,
        approve: bool,
        risk_event_id: int = 1,
        halted: str | None = None,
        admit_refusal: AccountRejection | None = None,
    ) -> None:
        self._approve = approve
        self._risk_event_id = risk_event_id
        self.halted = halted
        self.admit_refusal = admit_refusal
        self.evaluated: list[str] = []
        self.admitted: list[str] = []
        self.post_traded: list[str] = []
        self.discarded: list[str] = []
        self.overloaded: list[tuple[str, int]] = []

    def halted_reason(self) -> str | None:
        return self.halted

    async def evaluate(
        self, _signal: Signal, *, intent_id: str, opportunity_uid: uuid.UUID | None, is_shadow: bool
    ) -> _FakeVerdict:
        self.evaluated.append(intent_id)
        if self._approve:
            return _FakeVerdict(True, self._risk_event_id, RiskDecision.APPROVED)
        return _FakeVerdict(False, None, RiskDecision.REJECTED)

    async def admit(
        self, _signal: Signal, *, intent_id: str, opportunity_uid: uuid.UUID | None, is_shadow: bool
    ) -> AccountRejection | None:
        self.admitted.append(intent_id)
        return self.admit_refusal

    def record_discarded(
        self,
        *,
        intent_id: str,
        opportunity_uid: uuid.UUID | None,
        is_shadow: bool,
        strategy: str | None,
        reason: str,
    ) -> None:
        self.discarded.append(intent_id)

    def record_queue_overload(
        self,
        *,
        intent_id: str,
        opportunity_uid: uuid.UUID | None,
        is_shadow: bool,
        strategy: str | None,
        queue_size: int,
    ) -> None:
        self.overloaded.append((intent_id, queue_size))

    async def evaluate_post_trade(
        self,
        _attempt: ExecutionAttempt,
        _signal: Signal,
        *,
        intent_id: str,
        opportunity_uid: uuid.UUID | None,
    ) -> None:
        self.post_traded.append(intent_id)
        return None


async def test_a_risk_rejected_signal_never_reaches_the_adapter_or_recorder() -> None:
    recorder = Recorder()
    adapter = Adapter()
    risk_engine = FakeRiskEngine(approve=False)
    dispatcher = ExecutionDispatcher(
        ExecutionCoordinator(adapter),
        recorder,  # type: ignore[arg-type]
        queue_size=2,
        workers=1,
        recent_attempts=2,
        timeout_ms=100,
        risk_engine=risk_engine,  # type: ignore[arg-type]
    )
    episode = uuid.uuid4()
    assert dispatcher.enqueue(signal(), episode, is_shadow=False)
    dispatcher.start()
    await dispatcher.stop()
    assert risk_engine.evaluated == [f"signal:{episode}"]
    assert adapter.submitted == []
    assert recorder.items == []


async def test_an_approved_signal_carries_its_risk_event_id_to_the_order_request() -> None:
    recorder = Recorder()
    adapter = Adapter()
    risk_engine = FakeRiskEngine(approve=True, risk_event_id=42)
    dispatcher = ExecutionDispatcher(
        ExecutionCoordinator(adapter),
        recorder,  # type: ignore[arg-type]
        queue_size=2,
        workers=1,
        recent_attempts=2,
        timeout_ms=100,
        risk_engine=risk_engine,  # type: ignore[arg-type]
    )
    assert dispatcher.enqueue(signal(), uuid.uuid4(), is_shadow=False)
    dispatcher.start()
    await asyncio.wait_for(recorder.recorded.wait(), timeout=1)
    await dispatcher.stop()
    attempt, _uid = recorder.items[0]
    assert attempt.buy.result.request.risk_event_id == 42
    assert attempt.sell.result.request.risk_event_id == 42
    assert risk_engine.post_traded == risk_engine.evaluated
    # Admission is checked between approval and submission, every time.
    assert risk_engine.admitted == risk_engine.evaluated


def _dispatcher(
    risk_engine: FakeRiskEngine,
    recorder: Recorder,
    adapter: Adapter,
    *,
    queue_size: int = 4,
) -> ExecutionDispatcher:
    return ExecutionDispatcher(
        ExecutionCoordinator(adapter),
        recorder,  # type: ignore[arg-type]
        queue_size=queue_size,
        workers=1,
        recent_attempts=4,
        timeout_ms=100,
        risk_engine=risk_engine,  # type: ignore[arg-type]
    )


async def test_admission_refused_after_approval_places_no_order() -> None:
    """The kill-switch race: approved, then halted before submission."""
    recorder = Recorder()
    adapter = Adapter()
    risk_engine = FakeRiskEngine(
        approve=True,
        admit_refusal=AccountRejection(RejectionCode.RISK_PAUSED, "halted mid-flight"),
    )
    dispatcher = _dispatcher(risk_engine, recorder, adapter)
    assert dispatcher.enqueue(signal(), uuid.uuid4(), is_shadow=False)
    dispatcher.start()
    await dispatcher.stop()
    assert risk_engine.admitted, "admission must be consulted before submitting"
    # Approved, but never sent: no adapter contact, and no order rows either.
    assert adapter.submitted == []
    assert recorder.items == []


async def test_actionable_work_is_refused_at_the_door_while_halted() -> None:
    recorder = Recorder()
    risk_engine = FakeRiskEngine(approve=True, halted="kill switch engaged")
    dispatcher = _dispatcher(risk_engine, recorder, Adapter())
    episode = uuid.uuid4()
    assert not dispatcher.enqueue(signal(), episode, is_shadow=False)
    assert dispatcher.halted == 1
    assert risk_engine.discarded == [f"signal:{episode}"]
    # A probe is research and is governed separately: it is still accepted.
    assert dispatcher.enqueue(signal(), uuid.uuid4(), is_shadow=True)


async def test_queue_overload_is_audited_against_the_queues_own_bound() -> None:
    """Defect 12: one authoritative queue size - the dispatcher's own."""
    recorder = Recorder()
    risk_engine = FakeRiskEngine(approve=True)
    dispatcher = _dispatcher(risk_engine, recorder, Adapter(), queue_size=1)
    assert dispatcher.enqueue(signal(), uuid.uuid4(), is_shadow=False)
    assert not dispatcher.enqueue(signal(), uuid.uuid4(), is_shadow=False)

    assert dispatcher.overflow == 1
    assert risk_engine.overloaded[0][1] == 1, "the bound reported is the real queue size"


async def test_a_kill_purges_queued_actionable_work_but_keeps_probes() -> None:
    recorder = Recorder()
    risk_engine = FakeRiskEngine(approve=True)
    dispatcher = _dispatcher(risk_engine, recorder, Adapter())
    actionable = uuid.uuid4()
    probe = uuid.uuid4()
    assert dispatcher.enqueue(signal(), actionable, is_shadow=False)
    assert dispatcher.enqueue(signal(), probe, is_shadow=True)

    dropped = dispatcher.purge("kill switch engaged")

    assert dropped == 1
    assert dispatcher.purged == 1
    assert risk_engine.discarded == [f"signal:{actionable}"]
    # The claim is released too, so the episode can be re-offered after a
    # re-arm rather than being silently deduplicated away forever.
    assert dispatcher.enqueue(signal(), actionable, is_shadow=False)


async def test_an_episode_is_enqueued_only_once() -> None:
    recorder = Recorder()
    dispatcher = ExecutionDispatcher(
        ExecutionCoordinator(Adapter()),
        recorder,  # type: ignore[arg-type]
        queue_size=2,
        workers=1,
        recent_attempts=2,
        timeout_ms=100,
        risk_engine=FakeRiskEngine(approve=True),  # type: ignore[arg-type]
    )
    episode = uuid.uuid4()
    assert dispatcher.enqueue(signal(), episode, is_shadow=False)
    assert not dispatcher.enqueue(signal(), episode, is_shadow=False)
    dispatcher.start()
    await asyncio.wait_for(recorder.recorded.wait(), timeout=1)
    await dispatcher.stop()
    assert len(recorder.items) == 1
    assert dispatcher.duplicates == 1


async def test_queue_overflow_is_explicit_and_bounded() -> None:
    recorder = Recorder()
    dispatcher = ExecutionDispatcher(
        ExecutionCoordinator(Adapter()),
        recorder,  # type: ignore[arg-type]
        queue_size=1,
        workers=1,
        recent_attempts=2,
        timeout_ms=100,
        risk_engine=FakeRiskEngine(approve=True),  # type: ignore[arg-type]
    )
    assert dispatcher.enqueue(signal(), uuid.uuid4(), is_shadow=False)
    assert not dispatcher.enqueue(signal(), uuid.uuid4(), is_shadow=False)
    assert dispatcher.overflow == 1
    dispatcher.start()
    await dispatcher.stop()


class TestInlineExecution:
    """Replay's path: the same admission and processing, with no queue."""

    async def test_it_processes_now_and_reports_acceptance_like_enqueue(self) -> None:
        recorder, adapter = Recorder(), Adapter()
        approving = FakeRiskEngine(approve=True, risk_event_id=7)
        dispatcher = _dispatcher(approving, recorder, adapter, queue_size=1)
        episode = uuid.uuid4()
        assert await dispatcher.execute_inline(signal(), episode, is_shadow=False)
        # Done before returning: no worker was started, nothing was queued.
        assert len(recorder.items) == 1 and dispatcher.attempts
        assert approving.admitted == approving.evaluated == approving.post_traded
        # One intent per episode, exactly as enqueue enforces it.
        assert not await dispatcher.execute_inline(signal(), episode, is_shadow=False)
        assert dispatcher.duplicates == 1

    async def test_a_risk_refusal_is_still_accepted_work(self) -> None:
        """Acceptance, not approval: the strategy loop marks the episode either way."""
        recorder, adapter = Recorder(), Adapter()
        dispatcher = _dispatcher(FakeRiskEngine(approve=False), recorder, adapter)
        assert await dispatcher.execute_inline(signal(), uuid.uuid4(), is_shadow=False)
        assert recorder.items == []

    async def test_halted_work_is_refused_at_the_door(self) -> None:
        recorder, adapter = Recorder(), Adapter()
        halted = FakeRiskEngine(approve=True, halted="kill switch engaged")
        dispatcher = _dispatcher(halted, recorder, adapter)
        assert not await dispatcher.execute_inline(signal(), uuid.uuid4(), is_shadow=False)
        assert halted.discarded and halted.evaluated == []

    @pytest.mark.parametrize("stage", ["risk", "execution", "recording", "post_trade"])
    async def test_an_unexpected_exception_propagates_instead_of_being_swallowed(
        self, stage: str
    ) -> None:
        """Before: logged as ``worker_failed`` and reported as accepted work."""
        recorder, adapter = Recorder(), Adapter()
        risk = FakeRiskEngine(approve=True)
        dispatcher = ExecutionDispatcher(
            ExecutionCoordinator(adapter, propagate_adapter_errors=True),
            recorder,  # type: ignore[arg-type]
            queue_size=4,
            workers=1,
            recent_attempts=4,
            timeout_ms=100,
            risk_engine=risk,  # type: ignore[arg-type]
        )

        def boom(*_: object, **__: object) -> None:
            raise RuntimeError(f"{stage} broke")

        async def async_boom(*_: object, **__: object) -> None:
            boom()

        if stage == "risk":
            risk.evaluate = async_boom  # type: ignore[method-assign,assignment]
        elif stage == "execution":
            adapter.submit = async_boom  # type: ignore[method-assign,assignment]
        elif stage == "recording":
            recorder.record = boom  # type: ignore[method-assign,assignment]
        else:
            risk.evaluate_post_trade = async_boom  # type: ignore[method-assign,assignment]
        with pytest.raises(RuntimeError, match=f"{stage} broke"):
            await dispatcher.execute_inline(signal(), uuid.uuid4(), is_shadow=False)

    async def test_live_workers_still_survive_the_same_exception(self) -> None:
        recorder, adapter = Recorder(), Adapter()
        risk = FakeRiskEngine(approve=True)
        dispatcher = _dispatcher(risk, recorder, adapter)

        async def async_boom(*_: object, **__: object) -> None:
            raise RuntimeError("risk broke")

        risk.evaluate = async_boom  # type: ignore[method-assign,assignment]
        assert dispatcher.enqueue(signal(), uuid.uuid4(), is_shadow=False)
        dispatcher.start()
        await dispatcher.stop()
        assert dispatcher._tasks == []  # drained and stopped, never crashed

    async def test_a_live_coordinator_still_records_an_adapter_error_as_a_failed_leg(
        self,
    ) -> None:
        recorder, adapter = Recorder(), Adapter()

        async def refuse(*_: object, **__: object) -> None:
            raise ConnectionError("venue unreachable")

        adapter.submit = refuse  # type: ignore[method-assign,assignment]
        dispatcher = _dispatcher(FakeRiskEngine(approve=True), recorder, adapter)
        assert await dispatcher.execute_inline(signal(), uuid.uuid4(), is_shadow=False)
        ((attempt, _),) = recorder.items
        assert {leg.result.rejection for leg in attempt.legs} == {RejectionCode.ADAPTER_ERROR}
