"""Moving recorded events into the replay, and keeping the run's row alive.

``Feeder`` pulls validated batches into the market view only as far as a read
needs - never the whole range - so memory stays bounded by one page per table
plus the lookahead an order's latency requires.

``HeartbeatTask`` writes the run's liveness on a wall-clock timer, on its own
task. Replay progress cannot starve it: a long drain, a large flush or a slow
final snapshot still heart-beats, so orphan recovery never mistakes a slow run
for a dead one. It touches only the run's own row, never replay state, so it
cannot change what the replay computes; while its query is out the replay loop
merely holds virtual time still, as it does for any database I/O.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta

from trading_bot.backtest.clock import ReplayClock
from trading_bot.backtest.market_state import ReplayMarketData
from trading_bot.backtest.runs import Heartbeat, RunProgress, RunStore
from trading_bot.backtest.source import (
    DatasetRequest,
    EventValidator,
    HistoricalDataSource,
    Lookback,
    ReplayBatch,
)
from trading_bot.core.config import Settings
from trading_bot.core.logging import get_logger
from trading_bot.execution.paper import Sleeper

logger = get_logger(__name__)


class Feeder:
    def __init__(
        self,
        market: ReplayMarketData,
        batches: AsyncIterator[ReplayBatch],
        validator: EventValidator,
    ) -> None:
        self._market = market
        self._batches = batches
        self._validator = validator
        self._lock = asyncio.Lock()

    async def ensure_covers(self, moment: datetime) -> None:
        async with self._lock:
            while not self._market.covers(moment):
                batch = await anext(self._batches, None)
                if batch is None:
                    self._market.mark_exhausted()
                    return
                for kind, detail in batch.corrupt:
                    self._validator.corrupt(kind, detail)
                self._market.extend(
                    event for event in batch.events if self._validator.accept(event)
                )


def arrival_sleep(clock: ReplayClock, feeder: Callable[[], Feeder | None]) -> Sleeper:
    """The paper adapter's ``sleep``: buffer through the arrival, then wait.

    An order's latency moves the clock mid-execution, and the book it reads at
    arrival must already be buffered - so the feeder pulls through the arrival
    instant *before* the virtual sleep begins.
    """

    async def sleep(seconds: float) -> None:
        current = feeder()
        if current is not None and seconds > 0:
            await current.ensure_covers(clock.now() + timedelta(seconds=seconds))
        await clock.sleep(seconds)

    return sleep


async def open_replay(
    source: HistoricalDataSource,
    request: DatasetRequest,
    settings: Settings,
    market: ReplayMarketData,
    validator: EventValidator,
) -> Feeder:
    """Apply the state in force at the start, then open the in-window stream.

    Each kind looks back no further than the replay would carry it: a quote
    as far as the market-silence rule, a book and a funding observation as
    far as their carry limits. Anything older would be withdrawn at the first
    read anyway, so preloading it could only hide a gap.
    """
    lookback = Lookback(
        quote=timedelta(milliseconds=settings.market_data.market_silence_ms),
        book=timedelta(milliseconds=settings.backtest.max_book_carry_ms),
        funding=timedelta(milliseconds=settings.backtest.max_funding_carry_ms),
    )
    initial = await source.initial_state(request, lookback)
    for kind, detail in initial.corrupt:
        validator.corrupt(kind, detail)
    market.initialize(validator.initialize(initial.events))
    return Feeder(market, source.stream(request), validator)


class HeartbeatTask:
    def __init__(
        self,
        runs: RunStore,
        run_id: int,
        progress: RunProgress,
        *,
        interval_seconds: float,
        on_cancel: Callable[[], None],
        on_lost: Callable[[], None],
    ) -> None:
        self._runs = runs
        self._run_id = run_id
        self._progress = progress
        self._interval = interval_seconds
        self._on_cancel = on_cancel
        self._on_lost = on_lost
        self._task: asyncio.Task[None] | None = None
        self.beats = 0
        self.failures = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(
                self._beat(), name=f"backtest-heartbeat:{self._run_id}"
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                state = await self._runs.heartbeat(self._run_id, self._progress)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A missed beat is survivable; enough of them and orphan
                # recovery fails the run, which the next successful beat
                # reports as LOST.
                self.failures += 1
                logger.warning("backtest.heartbeat_failed", run_id=self._run_id, error=str(exc))
                continue
            self.beats += 1
            if state is Heartbeat.CANCEL_REQUESTED:
                self._on_cancel()
            elif state is Heartbeat.LOST:
                self._on_lost()
                return
