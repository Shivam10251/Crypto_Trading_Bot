"""A WebSocket connection that stays up.

Venue-independent: it knows nothing about Binance or market data, only how to
keep one stream of messages flowing and how to say so when it cannot.

- Reconnection with capped exponential backoff and jitter, so a venue outage
  does not become a reconnect storm (and then an IP ban).
- Heartbeat: protocol ping/pong through the ``websockets`` library. Binance
  pings every 20 s (spot) or 3 min (futures) and drops clients that miss a
  pong; the library answers automatically, and our own pings detect a dead TCP
  path that would otherwise look open for minutes.
- Idle timeout: a connection that is open but silent is treated as dead. This
  is not hypothetical - Binance futures accept a @ticker subscription on the
  legacy route and then never send a single message.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from trading_bot.core.logging import get_logger

logger = get_logger(__name__)

# Room for a burst of diff-depth levels on a volatile futures book; the
# library's 1 MiB default is closer to that than is comfortable.
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class WebSocketLike(Protocol):
    """The one call a connection needs from a socket - trivial to fake."""

    async def recv(self) -> str | bytes: ...


Connector = Callable[[str], AbstractAsyncContextManager[WebSocketLike]]
MessageHandler = Callable[[str | bytes, datetime], None]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def websocket_connector(
    *,
    ping_interval_seconds: float,
    ping_timeout_seconds: float,
    open_timeout_seconds: float = 10.0,
) -> Connector:
    """Real connections through the ``websockets`` library.

    A missed pong closes the socket, which surfaces as ``ConnectionClosed`` and
    triggers a reconnect - that is the heartbeat.
    """

    def connect(url: str) -> AbstractAsyncContextManager[WebSocketLike]:
        return ws_connect(
            url,
            ping_interval=ping_interval_seconds,
            ping_timeout=ping_timeout_seconds,
            open_timeout=open_timeout_seconds,
            close_timeout=5,
            max_size=MAX_MESSAGE_BYTES,
        )

    return connect


class ConnectionState(StrEnum):
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class ConnectionEvent:
    name: str
    state: ConnectionState
    at: datetime
    # Successful connections so far; above 1 on CONNECTED means a reconnect.
    connection_number: int
    reason: str | None = None
    retry_in_seconds: float | None = None


StateHandler = Callable[[ConnectionEvent], None]


@dataclass(slots=True)
class Backoff:
    """Capped exponential backoff with jitter."""

    initial_seconds: float = 0.5
    max_seconds: float = 30.0
    jitter: bool = True
    attempts: int = 0

    def next_delay(self) -> float:
        # The exponent is capped so a long outage cannot overflow the float.
        delay = min(self.max_seconds, self.initial_seconds * 2.0 ** min(self.attempts, 32))
        self.attempts += 1
        if self.jitter:
            # Spreads reconnects when many connections drop at once. Not
            # security-sensitive, so the stdlib generator is appropriate.
            delay *= random.uniform(0.5, 1.0)  # noqa: S311
        return delay

    def reset(self) -> None:
        self.attempts = 0


class StreamConnection:
    """Connect, read, and reconnect until cancelled."""

    def __init__(
        self,
        name: str,
        url: str,
        *,
        on_message: MessageHandler,
        connector: Connector,
        on_state: StateHandler | None = None,
        backoff: Backoff | None = None,
        idle_timeout_seconds: float = 10.0,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.name = name
        self.url = url
        self._on_message = on_message
        self._connector = connector
        self._on_state = on_state
        self._backoff = backoff or Backoff()
        self._idle_timeout = idle_timeout_seconds
        self._clock = clock
        self._sleep = sleep
        self.state = ConnectionState.DISCONNECTED
        self.connections = 0
        self.messages = 0
        self.last_message_at: datetime | None = None

    async def run(self) -> None:
        """Runs until the task is cancelled; network faults never escape."""
        try:
            while True:
                self._transition(ConnectionState.CONNECTING)
                reason = await self._session()
                delay = self._backoff.next_delay()
                logger.warning(
                    "ws.disconnected",
                    connection=self.name,
                    reason=reason,
                    retry_in_seconds=round(delay, 2),
                )
                self._transition(
                    ConnectionState.DISCONNECTED, reason=reason, retry_in_seconds=delay
                )
                await self._sleep(delay)
        finally:
            self._transition(ConnectionState.STOPPED)

    async def _session(self) -> str:
        """One connection's lifetime. Returns why it ended."""
        try:
            async with self._connector(self.url) as socket:
                self.connections += 1
                self._transition(ConnectionState.CONNECTED)
                logger.info("ws.connected", connection=self.name, number=self.connections)
                healthy = False
                while True:
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=self._idle_timeout)
                    except TimeoutError:
                        return f"no message for {self._idle_timeout:g}s"
                    received_at = self._clock()
                    if not healthy:
                        # Only a connection that delivers resets the backoff;
                        # one that opens and drops at once must keep backing off.
                        healthy = True
                        self._backoff.reset()
                    self.messages += 1
                    self.last_message_at = received_at
                    self._on_message(raw, received_at)
        except TimeoutError:
            return "connect timed out"
        except (OSError, WebSocketException) as exc:
            return f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            # A bug in a handler must not end the feed for good; it is logged
            # loudly and the connection starts over.
            logger.exception("ws.handler_failed", connection=self.name)
            return f"unexpected {type(exc).__name__}: {exc}"

    def _transition(
        self,
        state: ConnectionState,
        *,
        reason: str | None = None,
        retry_in_seconds: float | None = None,
    ) -> None:
        self.state = state
        if self._on_state is None:
            return
        event = ConnectionEvent(
            name=self.name,
            state=state,
            at=self._clock(),
            connection_number=self.connections,
            reason=reason,
            retry_in_seconds=retry_in_seconds,
        )
        try:
            self._on_state(event)
        except Exception:
            logger.exception("ws.state_handler_failed", connection=self.name)
