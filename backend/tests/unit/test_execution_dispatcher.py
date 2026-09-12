"""Scheduling execution once per opportunity episode without blocking strategy."""

from __future__ import annotations

import asyncio
import uuid

from tests.unit.test_execution_coordinator import Adapter, signal
from trading_bot.execution.coordinator import ExecutionAttempt, ExecutionCoordinator
from trading_bot.execution.dispatcher import ExecutionDispatcher


class Recorder:
    def __init__(self) -> None:
        self.items: list[tuple[ExecutionAttempt, uuid.UUID | None]] = []
        self.recorded = asyncio.Event()

    def record(self, attempt: ExecutionAttempt, opportunity_uid: uuid.UUID | None = None) -> None:
        self.items.append((attempt, opportunity_uid))
        self.recorded.set()


async def test_an_episode_is_enqueued_only_once() -> None:
    recorder = Recorder()
    dispatcher = ExecutionDispatcher(
        ExecutionCoordinator(Adapter()),
        recorder,  # type: ignore[arg-type]
        queue_size=2,
        workers=1,
        recent_attempts=2,
        timeout_ms=100,
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
    )
    assert dispatcher.enqueue(signal(), uuid.uuid4(), is_shadow=False)
    assert not dispatcher.enqueue(signal(), uuid.uuid4(), is_shadow=False)
    assert dispatcher.overflow == 1
    dispatcher.start()
    await dispatcher.stop()
