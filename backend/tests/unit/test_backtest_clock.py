"""Virtual time: one clock, advanced only by the replay, and never past a sleeper."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

from trading_bot.backtest.clock import (
    ReplayClock,
    ReplayInvariantError,
    from_micros,
    to_micros,
)
from trading_bot.backtest.loop import ReplayEventLoop, guard_sessions, run_replay

START = datetime(2026, 9, 1, 12, tzinfo=UTC)


class TestClock:
    def test_micros_round_trip_exactly(self) -> None:
        moment = datetime(2026, 9, 1, 23, 59, 59, 999_999, tzinfo=UTC)
        assert from_micros(to_micros(moment)) == moment

    def test_naive_timestamps_are_refused(self) -> None:
        with pytest.raises(ReplayInvariantError):
            to_micros(datetime(2026, 9, 1))  # a naive timestamp is the point

    def test_time_never_moves_backwards(self) -> None:
        clock = ReplayClock(START)
        clock.advance_to(START + timedelta(seconds=1))
        with pytest.raises(ReplayInvariantError, match="backwards"):
            clock.advance_to(START)

    def test_the_clock_is_injectable_as_a_callable(self) -> None:
        clock = ReplayClock(START)
        assert clock() == clock.now() == START


class TestSleepers:
    def test_concurrent_legs_each_arrive_at_their_own_instant(self) -> None:
        clock = ReplayClock(START)
        arrivals: dict[str, tuple[datetime, datetime]] = {}

        async def leg(name: str, latency: float) -> None:
            sent = clock.now()
            await clock.sleep(latency)
            arrivals[name] = (sent, clock.now())

        async def main() -> None:
            await asyncio.gather(leg("spot", 0.137), leg("perp", 0.100))

        run_replay(main(), clock)
        # Both were sent at the same instant: neither measured its latency
        # from the other's arrival.
        assert arrivals["spot"] == (START, START + timedelta(milliseconds=137))
        assert arrivals["perp"] == (START, START + timedelta(milliseconds=100))

    def test_simultaneous_wakes_resume_in_registration_order(self) -> None:
        clock = ReplayClock(START)
        order: list[str] = []

        async def sleeper(name: str) -> None:
            await clock.sleep(0.05)
            order.append(name)

        async def main() -> None:
            await asyncio.gather(*(sleeper(name) for name in ("a", "b", "c")))

        run_replay(main(), clock)
        assert order == ["a", "b", "c"]

    def test_virtual_hours_pass_without_real_waiting(self) -> None:
        clock = ReplayClock(START)
        began = time.perf_counter()

        async def main() -> None:
            # A real-time guard around a virtual hour: it must not fire.
            await asyncio.wait_for(clock.sleep(3600), timeout=5)

        run_replay(main(), clock)
        assert clock.now() == START + timedelta(hours=1)
        assert time.perf_counter() - began < 2

    def test_the_driver_cannot_jump_past_a_pending_sleeper(self) -> None:
        clock = ReplayClock(START)

        async def main() -> None:
            task = asyncio.ensure_future(clock.sleep(1))
            await asyncio.sleep(0)
            with pytest.raises(ReplayInvariantError, match="past a sleeper"):
                clock.advance_to(START + timedelta(seconds=2))
            await task

        run_replay(main(), clock)

    def test_a_sleeper_does_not_wake_while_database_io_is_in_flight(self) -> None:
        """A leg must not arrive while another flow still has a query out."""
        clock = ReplayClock(START)
        observed: list[datetime] = []

        class _Session:
            async def __aenter__(self) -> _Session:
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

        async def slow_query() -> None:
            async with guard_sessions(lambda: _Session())():  # type: ignore[arg-type,return-value]
                # Real I/O would park here without being runnable.
                await asyncio.sleep(0.05)
                observed.append(clock.now())

        async def main() -> None:
            await asyncio.gather(clock.sleep(1), slow_query())

        run_replay(main(), clock)
        assert observed == [START], "virtual time advanced during in-flight I/O"
        assert clock.now() == START + timedelta(seconds=1)

    def test_cancelled_sleepers_do_not_move_time(self) -> None:
        clock = ReplayClock(START)

        async def main() -> None:
            task = asyncio.ensure_future(clock.sleep(10))
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await clock.sleep(0.5)

        run_replay(main(), clock)
        assert clock.now() == START + timedelta(milliseconds=500)

    def test_the_replay_loop_is_a_selector_loop(self) -> None:
        loop = ReplayEventLoop(ReplayClock(START))
        try:
            assert isinstance(loop, asyncio.SelectorEventLoop)
        finally:
            loop.close()
