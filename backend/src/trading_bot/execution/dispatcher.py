"""Bounded, idempotent scheduling between strategy evaluation and execution."""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Final

from trading_bot.core.logging import get_logger
from trading_bot.execution.coordinator import ExecutionAttempt, ExecutionCoordinator
from trading_bot.execution.recorder import ExecutionRecorder
from trading_bot.strategy.models import Signal

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionWork:
    signal: Signal
    opportunity_uid: uuid.UUID
    intent_id: str
    is_shadow: bool


class ExecutionDispatcher:
    """Keep slow execution off the strategy loop and one-shot per episode.

    The episode UUID is fixed when the episode opens.  It therefore provides
    stable intent identity across every evaluation in that episode, while a
    new episode receives a new identity.  Claims are released only after the
    tracker says an episode ended, keeping memory bounded by live episodes and
    the queue rather than by process lifetime.
    """

    _STOP_TIMEOUT_MULTIPLIER: Final[int] = 2

    def __init__(
        self,
        coordinator: ExecutionCoordinator,
        recorder: ExecutionRecorder,
        *,
        queue_size: int,
        workers: int,
        recent_attempts: int,
        timeout_ms: int,
    ) -> None:
        self._coordinator = coordinator
        self._recorder = recorder
        self._queue: asyncio.Queue[ExecutionWork] = asyncio.Queue(maxsize=queue_size)
        self._worker_count = workers
        self._timeout_seconds = timeout_ms / 1000
        self._tasks: list[asyncio.Task[None]] = []
        self._claimed: set[str] = set()
        self._episode_intents: dict[uuid.UUID, set[str]] = {}
        self.attempts: deque[ExecutionAttempt] = deque(maxlen=recent_attempts)
        self.enqueued = 0
        self.duplicates = 0
        self.overflow = 0

    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._worker(), name=f"execution-worker:{index}")
            for index in range(self._worker_count)
        ]

    def enqueue(
        self,
        signal: Signal,
        opportunity_uid: uuid.UUID,
        *,
        is_shadow: bool,
    ) -> bool:
        kind = "shadow" if is_shadow else "signal"
        intent_id = f"{kind}:{opportunity_uid}"
        if intent_id in self._claimed:
            self.duplicates += 1
            return False
        work = ExecutionWork(signal, opportunity_uid, intent_id, is_shadow)
        try:
            self._queue.put_nowait(work)
        except asyncio.QueueFull:
            self.overflow += 1
            logger.error(
                "execution.queue_full",
                intent_id=intent_id,
                queue_size=self._queue.maxsize,
            )
            return False
        self._claimed.add(intent_id)
        self._episode_intents.setdefault(opportunity_uid, set()).add(intent_id)
        self.enqueued += 1
        return True

    def release(self, opportunity_uids: set[uuid.UUID]) -> None:
        """Forget claims only once those episodes have definitely ended."""
        for opportunity_uid in opportunity_uids:
            for intent_id in self._episode_intents.pop(opportunity_uid, ()):
                self._claimed.discard(intent_id)

    async def stop(self) -> None:
        """Drain accepted work before cancelling idle workers."""
        if not self._tasks:
            return
        try:
            async with asyncio.timeout(
                max(1.0, self._timeout_seconds * self._STOP_TIMEOUT_MULTIPLIER)
            ):
                await self._queue.join()
        except TimeoutError:
            logger.error("execution.shutdown_drain_timeout", pending=self._queue.qsize())
        finally:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

    async def _worker(self) -> None:
        while True:
            work = await self._queue.get()
            try:
                attempt = await self._coordinator.execute(
                    work.signal,
                    is_shadow=work.is_shadow,
                    execution_intent_id=work.intent_id,
                )
                if attempt is not None:
                    self.attempts.append(attempt)
                    self._recorder.record(attempt, work.opportunity_uid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a worker must survive one malformed signal
                logger.exception(
                    "execution.worker_failed", intent_id=work.intent_id, error=str(exc)
                )
            finally:
                self._queue.task_done()
