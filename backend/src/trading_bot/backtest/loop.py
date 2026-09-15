"""An event loop that lets virtual sleepers wake only when nothing else can run.

The paper adapter submits both legs of an entry concurrently, each sleeping
its own simulated latency, and then reads the book at its own arrival. Under
replay those sleeps are virtual, and the danger is waking one before the
other has even registered: the second leg would then measure its latency from
the first leg's arrival. The fix is to advance virtual time only when the
process is *quiescent* - which is precisely when an asyncio loop is about to
block in ``select``:

- ``_run_once`` calls ``select(timeout)`` with ``timeout == 0`` whenever a
  callback is ready, so a non-zero timeout means nothing is runnable;
- database I/O is the one thing that can still be in flight, and every
  replay session is opened through ``guard_sessions``, which counts it.

So the selector wakes the earliest virtual sleeper only if it was asked to
wait, nothing is ready on a socket, and no session is open. Otherwise it is an
ordinary selector. Wall-clock timers keep their real meaning - asyncpg's own
timeouts, the paper adapter's hang guard - because the loop's ``time()`` is
untouched; only the replay's datetime clock is virtual.

Determinism depends on the replay path doing its database I/O one flow at a
time, which ``BacktestEngine`` guarantees by awaiting each step to completion.
"""

from __future__ import annotations

import asyncio
import selectors
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.backtest.clock import ReplayClock
from trading_bot.core.logging import get_logger

if TYPE_CHECKING:
    from _typeshed import FileDescriptorLike

logger = get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# While a session is open and a sleeper waits, poll rather than block, so a
# session held across a virtual sleep - a bug - is reported instead of hanging.
_BUSY_POLL_SECONDS = 0.5
_STALL_WARNING_SECONDS = 30.0


class _QuiescentSelector(selectors.BaseSelector):
    def __init__(self, inner: selectors.BaseSelector, loop: ReplayEventLoop) -> None:
        self._inner = inner
        self._loop = loop
        self._busy_since: float | None = None

    def register(
        self, fileobj: FileDescriptorLike, events: int, data: Any = None
    ) -> selectors.SelectorKey:
        return self._inner.register(fileobj, events, data)

    def unregister(self, fileobj: FileDescriptorLike) -> selectors.SelectorKey:
        return self._inner.unregister(fileobj)

    def modify(
        self, fileobj: FileDescriptorLike, events: int, data: Any = None
    ) -> selectors.SelectorKey:
        return self._inner.modify(fileobj, events, data)

    def get_map(self) -> Mapping[FileDescriptorLike, selectors.SelectorKey]:
        return self._inner.get_map()

    def close(self) -> None:
        self._inner.close()

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        clock = self._loop.clock
        if clock is None or (timeout is not None and timeout <= 0) or not clock.has_sleepers:
            self._busy_since = None
            return self._inner.select(timeout)
        ready = self._inner.select(0)
        if ready:
            return ready
        if self._loop.io_in_flight:
            now = time.monotonic()
            if self._busy_since is None:
                self._busy_since = now
            elif now - self._busy_since > _STALL_WARNING_SECONDS:
                logger.warning(
                    "backtest.replay_stalled",
                    detail="a database session stayed open while virtual sleepers waited",
                )
                self._busy_since = now
            wait = _BUSY_POLL_SECONDS if timeout is None else min(timeout, _BUSY_POLL_SECONDS)
            return self._inner.select(wait)
        self._busy_since = None
        clock.wake_next()
        return []


class ReplayEventLoop(asyncio.SelectorEventLoop):
    """A selector loop whose idle moments advance a ``ReplayClock``."""

    def __init__(self, clock: ReplayClock | None = None) -> None:
        self.clock = clock
        self._io_depth = 0
        super().__init__(_QuiescentSelector(selectors.DefaultSelector(), self))

    @property
    def io_in_flight(self) -> bool:
        return self._io_depth > 0

    def io_started(self) -> None:
        self._io_depth += 1

    def io_finished(self) -> None:
        self._io_depth -= 1
        if self._io_depth < 0:  # pragma: no cover - a guard, not a path
            raise RuntimeError("replay I/O depth went negative")


@asynccontextmanager
async def replay_io() -> AsyncIterator[None]:
    """Count the enclosed work as in-flight I/O; inert under any other loop.

    For I/O that is not a session - one query on a connection held for a
    whole run, which must not count as in flight *between* its queries or
    no virtual sleeper could ever wake.
    """
    loop = asyncio.get_running_loop()
    replay = loop if isinstance(loop, ReplayEventLoop) else None
    if replay is not None:
        replay.io_started()
    try:
        yield
    finally:
        if replay is not None:
            replay.io_finished()


def guard_sessions(factory: SessionFactory) -> SessionFactory:
    """Count every session opened through ``factory`` as in-flight I/O.

    Under any other loop the wrapper is inert, so the same stores can be
    built with it in tests that run on an ordinary loop.
    """

    @asynccontextmanager
    async def guarded() -> AsyncIterator[AsyncSession]:
        async with replay_io(), factory() as session:
            yield session

    return guarded


def run_replay[T](main: Coroutine[Any, Any, T], clock: ReplayClock) -> T:
    """Run ``main`` to completion on a replay loop bound to ``clock``.

    ``asyncio.Runner`` cancels ``main`` on the first Ctrl-C, which is what
    lets an interrupted backtest record itself as cancelled.
    """
    with asyncio.Runner(loop_factory=lambda: ReplayEventLoop(clock)) as runner:
        return runner.run(main)
