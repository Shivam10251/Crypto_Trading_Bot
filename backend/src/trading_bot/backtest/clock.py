"""Virtual time for replay: one clock, advanced only by the replay itself.

Every time-dependent component in the pipeline already takes an injected
``clock`` - the runner, monitor, risk engine, kill switch, coordinator, paper
adapter, closer, P&L source and portfolio service - and the paper adapter
takes an injected ``sleep``. A backtest hands all of them this one object, so
"now" means the same instant everywhere and never the wall clock.

Time moves in exactly two ways:

- ``advance_to`` - the replay driver moving to the next event or tick. It
  refuses to jump past a pending sleeper, which would let a submitted order
  arrive after events it should have preceded.
- ``sleep`` - a component waiting, such as an order's simulated latency. The
  sleeper is parked; ``ReplayEventLoop`` wakes the earliest one only when
  nothing else in the process can run and no database I/O is in flight. So
  two legs submitted together each arrive at their own simulated instant, and
  both registered their sleeps before either woke.

Ties are broken by registration order, so two sleepers waking at the same
microsecond resume in the order they went to sleep.
"""

from __future__ import annotations

import asyncio
import heapq
from datetime import UTC, datetime, timedelta

_MICROS = 1_000_000


class ReplayInvariantError(RuntimeError):
    """The replay was asked to do something that would break virtual time."""


def to_micros(moment: datetime) -> int:
    """Microseconds since the Unix epoch, exactly."""
    if moment.tzinfo is None:
        raise ReplayInvariantError("replay timestamps must be timezone-aware")
    delta = moment.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * _MICROS + delta.microseconds


def from_micros(value: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value)


class ReplayClock:
    """The only source of "now" inside a backtest."""

    def __init__(self, start: datetime) -> None:
        self._now = to_micros(start)
        self._sleepers: list[tuple[int, int, asyncio.Future[None]]] = []
        self._sequence = 0

    # --- reading --------------------------------------------------------

    def __call__(self) -> datetime:
        """So the clock itself can be injected wherever ``clock()`` is called."""
        return from_micros(self._now)

    def now(self) -> datetime:
        return from_micros(self._now)

    @property
    def now_micros(self) -> int:
        return self._now

    @property
    def has_sleepers(self) -> bool:
        self._discard_cancelled()
        return bool(self._sleepers)

    @property
    def next_wake(self) -> datetime | None:
        self._discard_cancelled()
        return from_micros(self._sleepers[0][0]) if self._sleepers else None

    # --- moving ---------------------------------------------------------

    def advance_to(self, moment: datetime) -> None:
        """Move to ``moment``. Never backwards, never past a sleeper."""
        target = to_micros(moment)
        if target < self._now:
            raise ReplayInvariantError(
                f"virtual time cannot move backwards: {moment.isoformat()} < {self.now()}"
            )
        wake = self.next_wake
        if wake is not None and to_micros(wake) < target:
            raise ReplayInvariantError(
                f"cannot advance to {moment.isoformat()} past a sleeper due at {wake.isoformat()}"
            )
        self._now = target

    async def sleep(self, seconds: float) -> None:
        """Wait ``seconds`` of virtual time. Zero yields without moving time."""
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        wake = self._now + round(seconds * _MICROS)
        heapq.heappush(self._sleepers, (wake, self._sequence, future))
        self._sequence += 1
        await future

    def wake_next(self) -> bool:
        """Advance to the earliest sleeper and wake everything due then.

        Called by ``ReplayEventLoop`` when the process is otherwise idle.
        Returns ``False`` when there was nothing to wake.
        """
        self._discard_cancelled()
        if not self._sleepers:
            return False
        wake = self._sleepers[0][0]
        self._now = max(self._now, wake)
        while self._sleepers and self._sleepers[0][0] <= self._now:
            _, _, future = heapq.heappop(self._sleepers)
            if not future.done():
                future.set_result(None)
        return True

    def _discard_cancelled(self) -> None:
        while self._sleepers and self._sleepers[0][2].done():
            heapq.heappop(self._sleepers)
