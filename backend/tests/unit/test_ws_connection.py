"""The reconnecting WebSocket connection, driven by a scripted fake socket."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from trading_bot.marketdata.connection import (
    Backoff,
    ConnectionEvent,
    ConnectionState,
    StreamConnection,
)


class FakeSocket:
    """Delivers scripted items, raising any exception among them, then goes silent."""

    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)

    async def recv(self) -> str | bytes:
        if not self._items:
            await asyncio.Event().wait()
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[no-any-return]


class FakeConnector:
    """Each connection attempt consumes the next scripted session.

    A session is a list of messages (with exceptions to raise mid-stream), or an
    exception to raise while connecting. When the script runs out, connections
    open and stay silent.
    """

    def __init__(self, *sessions: Any) -> None:
        self._sessions = list(sessions)
        self.attempts = 0

    def __call__(self, url: str) -> contextlib.AbstractAsyncContextManager[FakeSocket]:
        self.attempts += 1
        session = self._sessions.pop(0) if self._sessions else []
        return self._open(session)

    @asynccontextmanager
    async def _open(self, session: Any) -> AsyncIterator[FakeSocket]:
        if isinstance(session, BaseException):
            raise session
        yield FakeSocket(session)


class Recorder:
    def __init__(self) -> None:
        self.messages: list[str | bytes] = []
        self.stamps: list[datetime] = []
        self.events: list[ConnectionEvent] = []

    def on_message(self, raw: str | bytes, received_at: datetime) -> None:
        self.messages.append(raw)
        self.stamps.append(received_at)

    def on_state(self, event: ConnectionEvent) -> None:
        self.events.append(event)

    def states(self) -> list[ConnectionState]:
        return [event.state for event in self.events]

    def reasons(self) -> list[str]:
        return [
            event.reason
            for event in self.events
            if event.state is ConnectionState.DISCONNECTED and event.reason
        ]


async def eventually(predicate: Callable[[], bool], within: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


@asynccontextmanager
async def running(
    connector: FakeConnector,
    *,
    on_message: Callable[[str | bytes, datetime], None] | None = None,
    idle_timeout: float = 10.0,
) -> AsyncIterator[tuple[StreamConnection, Recorder, list[float]]]:
    recorder = Recorder()
    delays: list[float] = []

    async def no_wait(seconds: float) -> None:
        delays.append(seconds)
        await asyncio.sleep(0)

    connection = StreamConnection(
        "test",
        "wss://example.test/stream",
        on_message=on_message or recorder.on_message,
        on_state=recorder.on_state,
        connector=connector,
        backoff=Backoff(initial_seconds=0.5, max_seconds=4.0, jitter=False),
        idle_timeout_seconds=idle_timeout,
        sleep=no_wait,
    )
    task = asyncio.create_task(connection.run())
    try:
        yield connection, recorder, delays
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestDelivery:
    async def test_messages_arrive_in_order_with_timestamps(self) -> None:
        async with running(FakeConnector(["a", "b", "c"])) as (connection, recorder, _):
            await eventually(lambda: len(recorder.messages) == 3)
        assert recorder.messages == ["a", "b", "c"]
        assert all(stamp.tzinfo is not None for stamp in recorder.stamps)
        assert recorder.states()[:2] == [ConnectionState.CONNECTING, ConnectionState.CONNECTED]
        assert connection.messages == 3
        assert connection.last_message_at == recorder.stamps[-1]

    async def test_cancelling_reports_stopped(self) -> None:
        async with running(FakeConnector(["a"])) as (_, recorder, _):
            await eventually(lambda: recorder.messages == ["a"])
        assert recorder.states()[-1] is ConnectionState.STOPPED


class TestReconnection:
    async def test_reconnects_after_the_socket_drops(self) -> None:
        connector = FakeConnector(["a", OSError("connection reset")], ["b"])
        async with running(connector) as (_, recorder, _):
            await eventually(lambda: recorder.messages == ["a", "b"])
        assert connector.attempts == 2
        assert recorder.reasons()[0] == "OSError: connection reset"
        connected = [e for e in recorder.events if e.state is ConnectionState.CONNECTED]
        assert [e.connection_number for e in connected] == [1, 2]

    async def test_a_silent_connection_is_treated_as_dead(self) -> None:
        """Open but silent - how Binance futures treat @ticker on the legacy route."""
        async with running(FakeConnector(["a"], ["b"]), idle_timeout=0.05) as (_, recorder, _):
            await eventually(lambda: recorder.messages == ["a", "b"])
        assert recorder.reasons()[0] == "no message for 0.05s"

    async def test_connect_timeout_is_reported(self) -> None:
        async with running(FakeConnector(TimeoutError(), ["ok"])) as (_, recorder, _):
            await eventually(lambda: recorder.messages == ["ok"])
        assert recorder.reasons()[0] == "connect timed out"

    async def test_a_handler_bug_restarts_the_connection_instead_of_ending_it(self) -> None:
        received: list[str | bytes] = []

        def fragile(raw: str | bytes, _at: datetime) -> None:
            if raw == "boom":
                raise RuntimeError("handler bug")
            received.append(raw)

        connector = FakeConnector(["boom"], ["fine"])
        async with running(connector, on_message=fragile) as (_, recorder, _):
            await eventually(lambda: received == ["fine"])
        assert recorder.reasons()[0] == "unexpected RuntimeError: handler bug"


class TestBackoff:
    async def test_delays_grow_exponentially_up_to_the_cap(self) -> None:
        connector = FakeConnector(*[OSError("refused")] * 5, ["ok"])
        async with running(connector) as (_, recorder, delays):
            await eventually(lambda: recorder.messages == ["ok"])
        assert delays == [0.5, 1.0, 2.0, 4.0, 4.0]

    async def test_a_session_that_delivers_resets_the_backoff(self) -> None:
        connector = FakeConnector(
            OSError("refused"),
            OSError("refused"),
            ["a", OSError("drop")],
            OSError("refused"),
            ["b"],
        )
        async with running(connector) as (_, recorder, delays):
            await eventually(lambda: recorder.messages == ["a", "b"])
        assert delays == [0.5, 1.0, 0.5, 1.0]

    async def test_opening_and_dropping_keeps_backing_off(self) -> None:
        """Resetting on connect alone would reconnect-storm a flapping venue."""
        connector = FakeConnector([OSError("drop")], [OSError("drop")], ["ok"])
        async with running(connector) as (_, recorder, delays):
            await eventually(lambda: recorder.messages == ["ok"])
        assert delays == [0.5, 1.0]

    def test_jitter_stays_between_half_and_the_full_delay(self) -> None:
        delays = [Backoff(initial_seconds=1.0, max_seconds=10.0).next_delay() for _ in range(50)]
        assert all(0.5 <= delay <= 1.0 for delay in delays)

    def test_a_long_outage_does_not_overflow(self) -> None:
        backoff = Backoff(initial_seconds=0.5, max_seconds=30.0, jitter=False, attempts=10_000)
        assert backoff.next_delay() == 30.0
