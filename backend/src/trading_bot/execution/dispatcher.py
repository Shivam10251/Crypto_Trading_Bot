"""Bounded, idempotent scheduling between strategy evaluation and execution."""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final

from trading_bot.core.logging import get_logger
from trading_bot.execution.coordinator import ExecutionAttempt, ExecutionCoordinator
from trading_bot.execution.recorder import ExecutionRecorder
from trading_bot.strategy.models import Signal

if TYPE_CHECKING:
    from trading_bot.risk.engine import RiskEngine

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
        risk_engine: RiskEngine,
    ) -> None:
        self._coordinator = coordinator
        self._recorder = recorder
        self._risk_engine = risk_engine
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
        self.halted = 0
        self.purged = 0
        # Cancellation runs on the event loop after a synchronous purge; held
        # so the task cannot be garbage collected mid-flight.
        self._cancellations: set[asyncio.Task[int]] = set()

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
        work = self._admit(signal, opportunity_uid, is_shadow=is_shadow)
        if work is None:
            return False
        try:
            self._queue.put_nowait(work)
        except asyncio.QueueFull:
            self.overflow += 1
            logger.error(
                "execution.queue_full",
                intent_id=work.intent_id,
                queue_size=self._queue.maxsize,
            )
            # Best-effort, not durable-before-return: nothing was ever queued
            # for this intent, so there is no order for the audit trail to
            # gate - unlike an approval, a late or lost write here risks
            # nothing except an incomplete research record.
            self._risk_engine.record_queue_overload(
                intent_id=work.intent_id,
                opportunity_uid=opportunity_uid,
                is_shadow=is_shadow,
                strategy=signal.strategy,
                queue_size=self._queue.maxsize,
            )
            return False
        self._claim(work)
        return True

    async def execute_inline(
        self,
        signal: Signal,
        opportunity_uid: uuid.UUID,
        *,
        is_shadow: bool,
    ) -> bool:
        """Admit and process one signal now, without the queue or a worker.

        For a deterministic replay, which must finish one decision before
        virtual time moves on. The admission rules - one intent per episode,
        refused while halted - and the processing are exactly ``enqueue`` and
        a worker's; only the queue, and so queue overload, does not exist.
        Returns what ``enqueue`` returns: whether the work was accepted, not
        whether risk approved it or anything filled.

        **An unexpected exception propagates.** A worker swallows one so the
        live process survives a malformed signal; a replay that did the same
        would carry on from a state no real run could reach - an approval
        without its order, a fill without its post-trade check - and publish
        the result as if nothing happened. The caller fails the run instead.
        """
        work = self._admit(signal, opportunity_uid, is_shadow=is_shadow)
        if work is None:
            return False
        self._claim(work)
        await self._process(work)
        return True

    def _admit(
        self, signal: Signal, opportunity_uid: uuid.UUID, *, is_shadow: bool
    ) -> ExecutionWork | None:
        kind = "shadow" if is_shadow else "signal"
        intent_id = f"{kind}:{opportunity_uid}"
        if intent_id in self._claimed:
            self.duplicates += 1
            return None
        # Refuse actionable work outright while trading is halted, rather than
        # queueing something a worker would only reject. Probes are research
        # and are governed separately.
        if not is_shadow:
            halted = self._risk_engine.halted_reason()
            if halted is not None:
                self.halted += 1
                self._risk_engine.record_discarded(
                    intent_id=intent_id,
                    opportunity_uid=opportunity_uid,
                    is_shadow=is_shadow,
                    strategy=signal.strategy,
                    reason=f"not accepted while trading is halted: {halted}",
                )
                return None
        return ExecutionWork(signal, opportunity_uid, intent_id, is_shadow)

    def _claim(self, work: ExecutionWork) -> None:
        self._claimed.add(work.intent_id)
        self._episode_intents.setdefault(work.opportunity_uid, set()).add(work.intent_id)
        self.enqueued += 1

    def release(self, opportunity_uids: set[uuid.UUID]) -> None:
        """Forget claims only once those episodes have definitely ended."""
        for opportunity_uid in opportunity_uids:
            for intent_id in self._episode_intents.pop(opportunity_uid, ()):
                self._claimed.discard(intent_id)

    def purge(self, reason: str) -> int:
        """Drop accepted-but-unsubmitted actionable work. Called on a kill.

        Synchronous on purpose: it is a kill-switch listener, and the queue
        operations it needs are all synchronous, so the work is gone before
        the next event-loop turn can hand any of it to a worker. Shadow
        probes stay queued - they are isolated from the actionable halt - and
        are put back in the order they were taken.

        Anything already submitted is handled separately, by asking the
        adapter to cancel it. Paper orders do not rest after ``submit``
        returns, but they are tracked during simulated latency and can still
        be cancelled in that interval.
        """
        kept: list[ExecutionWork] = []
        dropped = 0
        while True:
            try:
                work = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if work.is_shadow:
                kept.append(work)
                self._queue.task_done()
                continue
            dropped += 1
            self._claimed.discard(work.intent_id)
            self._risk_engine.record_discarded(
                intent_id=work.intent_id,
                opportunity_uid=work.opportunity_uid,
                is_shadow=False,
                strategy=work.signal.strategy,
                reason=f"dropped from the execution queue: {reason}",
            )
            self._queue.task_done()
        for work in kept:
            self._queue.put_nowait(work)
        self.purged += dropped
        if dropped:
            logger.warning("execution.queue_purged", dropped=dropped, reason=reason)
        self._cancel_in_flight(reason)
        return dropped

    def _cancel_in_flight(self, reason: str) -> None:
        """Ask the adapter to withdraw anything submitted and still open."""
        try:
            task = asyncio.get_running_loop().create_task(
                self._coordinator.cancel_in_flight(reason)
            )
        except RuntimeError:  # no loop: nothing can be in flight either
            return
        self._cancellations.add(task)
        task.add_done_callback(self._cancellations.discard)

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
            # A kill listener schedules adapter cancellation because the
            # listener itself must stay synchronous. Do not tear the service
            # down while those best-effort withdrawals are still in flight,
            # but keep shutdown bounded if an adapter never answers.
            cancellations = tuple(self._cancellations)
            if cancellations:
                try:
                    async with asyncio.timeout(max(1.0, self._timeout_seconds)):
                        await asyncio.gather(*cancellations, return_exceptions=True)
                except TimeoutError:
                    logger.error(
                        "execution.shutdown_cancel_timeout", pending=len(self._cancellations)
                    )
                    for cancellation in cancellations:
                        cancellation.cancel()
                    await asyncio.gather(*cancellations, return_exceptions=True)

    async def _worker(self) -> None:
        while True:
            work = await self._queue.get()
            try:
                await self._process(work)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a worker must survive one malformed signal
                logger.exception(
                    "execution.worker_failed", intent_id=work.intent_id, error=str(exc)
                )
            finally:
                self._queue.task_done()

    async def _process(self, work: ExecutionWork) -> None:
        """Risk approval, then execution, then post-trade review.

        Off the strategy loop by construction - this runs inside a worker -
        so a slow, durable risk-event write here never delays the next
        evaluation cycle. An order is never submitted unless the risk engine
        returns an approval whose ``risk_event_id`` proves it was stored.
        """
        verdict = await self._risk_engine.evaluate(
            work.signal,
            intent_id=work.intent_id,
            opportunity_uid=work.opportunity_uid,
            is_shadow=work.is_shadow,
        )
        if not verdict.is_approved:
            return
        risk_event_id = verdict.risk_event_id
        # Checked again inside the coordinator, immediately before the
        # adapter is handed anything: approval and submission are not the
        # same instant, and a kill can land between them.
        admission = partial(
            self._risk_engine.admit,
            work.signal,
            intent_id=work.intent_id,
            opportunity_uid=work.opportunity_uid,
            is_shadow=work.is_shadow,
        )
        attempt = await self._coordinator.execute(
            work.signal,
            is_shadow=work.is_shadow,
            execution_intent_id=work.intent_id,
            risk_event_id=risk_event_id,
            admission=admission,
        )
        if attempt is None:
            return
        self.attempts.append(attempt)
        self._recorder.record(attempt, work.opportunity_uid)
        await self._risk_engine.evaluate_post_trade(
            attempt,
            work.signal,
            intent_id=work.intent_id,
            opportunity_uid=work.opportunity_uid,
        )
