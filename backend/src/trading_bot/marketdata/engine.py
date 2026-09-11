"""The market-data engine: live, validated, normalized data for every market.

    WebSocket -> venue parser -> tracker / local book -> MarketSnapshot -> consumers

It owns everything venue-independent about streaming:
- one ``StreamConnection`` per endpoint the venue asks for (reconnect,
  heartbeat, idle timeout)
- a tracker per market: latest quote, 24h statistics, latency, counters
- local order books when depth is subscribed, kept honest by
  ``BookSynchronizer`` - consumers see ``SYNCING`` rather than a wrong book
- a watchdog that marks markets STALE when their data stops arriving

Consumers call ``snapshot(ref)`` or iterate ``updates()``. Neither exposes
anything about WebSockets, which keeps strategies testable without a network.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from trading_bot.core.config import MarketDataConfig
from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import Severity, SystemEventType
from trading_bot.exchange.errors import ExchangeDataError, ExchangeError, UnknownMarketError
from trading_bot.exchange.models import MarketDataSubscription, MarketRef, Quote, TickerStats
from trading_bot.exchange.streaming import MarketStreamSource, StreamEndpoint, StreamKind
from trading_bot.marketdata.book_sync import DISABLED_VIEW, BookSynchronizer, SnapshotFetcher
from trading_bot.marketdata.connection import (
    Backoff,
    ConnectionEvent,
    ConnectionState,
    Connector,
    MessageHandler,
    StreamConnection,
    websocket_connector,
)
from trading_bot.marketdata.models import (
    EngineHealth,
    FeedStatus,
    MarketDataEvent,
    MarketSnapshot,
)

logger = get_logger(__name__)

EventListener = Callable[[MarketDataEvent], None]

# After the first few, invalid messages are logged only this often, so one bad
# field in a hot stream cannot flood the log.
_INVALID_LOG_EVERY = 1000


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ms_between(later: datetime, earlier: datetime) -> int:
    return int((later - earlier).total_seconds() * 1000)


class _MarketTracker:
    """Mutable per-market state; only the engine touches it."""

    __slots__ = (
        "connected_at",
        "gaps",
        "last_update_at",
        "latency_ms",
        "quote",
        "ref",
        "resyncs",
        "stale",
        "ticker",
        "updates",
    )

    def __init__(self, ref: MarketRef) -> None:
        self.ref = ref
        self.quote: Quote | None = None
        self.ticker: TickerStats | None = None
        self.latency_ms: int | None = None
        self.last_update_at: datetime | None = None
        self.connected_at: datetime | None = None
        self.updates = 0
        self.gaps = 0
        self.resyncs = 0
        self.stale = False

    def touch(self, at: datetime, latency_ms: int | None) -> None:
        self.updates += 1
        self.last_update_at = at
        if latency_ms is not None:
            self.latency_ms = latency_ms


class _Subscriber:
    __slots__ = ("closed", "pending", "wake")

    def __init__(self) -> None:
        self.pending: dict[MarketRef, None] = {}
        self.wake = asyncio.Event()
        self.closed = False


class MarketDataEngine:
    """Live data for a set of markets on one venue."""

    def __init__(
        self,
        source: MarketStreamSource,
        subscription: MarketDataSubscription,
        config: MarketDataConfig,
        *,
        snapshot_fetcher: SnapshotFetcher | None = None,
        connector: Connector | None = None,
        max_backoff_seconds: float = 30.0,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if subscription.include_depth and snapshot_fetcher is None:
            raise ValueError("depth streaming needs a snapshot fetcher to seed the local book")
        self._source = source
        self._config = config
        self._connector = connector or websocket_connector(
            ping_interval_seconds=config.ping_interval_seconds,
            ping_timeout_seconds=config.ping_timeout_seconds,
        )
        self._max_backoff = max_backoff_seconds
        self._clock = clock

        self._endpoints = source.endpoints(subscription)
        refs = tuple(dict.fromkeys(subscription.refs))
        self._trackers = {ref: _MarketTracker(ref) for ref in refs}
        self._books: BookSynchronizer | None = None
        if snapshot_fetcher is not None and subscription.include_depth:
            self._books = BookSynchronizer(
                refs,
                depth_levels=subscription.depth_levels,
                config=config,
                fetch_snapshot=snapshot_fetcher,
                on_invalidated=self._book_invalidated,
                on_synced=self._book_synced,
                on_snapshot_failed=self._snapshot_failed,
            )
        self._endpoint_state = {e.name: ConnectionState.CONNECTING for e in self._endpoints}
        # Last message (or connect) per connection: the proof it is still alive.
        self._endpoint_heard: dict[str, datetime | None] = {e.name: None for e in self._endpoints}
        self._endpoint_by_name = {e.name: e for e in self._endpoints}
        self._ref_endpoints: dict[MarketRef, list[str]] = {ref: [] for ref in refs}
        for endpoint in self._endpoints:
            for ref in endpoint.refs:
                self._ref_endpoints.setdefault(ref, []).append(endpoint.name)

        self._listeners: list[EventListener] = []
        self._subscribers: set[_Subscriber] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False
        self._invalid_messages = 0

    # --- lifecycle --------------------------------------------------------

    @property
    def refs(self) -> tuple[MarketRef, ...]:
        return tuple(self._trackers)

    @property
    def endpoints(self) -> tuple[StreamEndpoint, ...]:
        return tuple(self._endpoints)

    def add_listener(self, listener: EventListener) -> None:
        """Receive infrastructure events (connects, gaps, staleness) as they happen."""
        self._listeners.append(listener)

    async def start(self) -> None:
        if self._running:
            raise RuntimeError("engine already started")
        self._running = True
        if self._books is not None:
            self._books.start()
        for endpoint in self._endpoints:
            connection = StreamConnection(
                endpoint.name,
                endpoint.url,
                on_message=self._handler_for(endpoint),
                on_state=self._on_connection_event,
                connector=self._connector,
                backoff=Backoff(
                    initial_seconds=self._config.reconnect_initial_backoff_seconds,
                    max_seconds=self._max_backoff,
                ),
                idle_timeout_seconds=self._config.idle_timeout_seconds,
                clock=self._clock,
            )
            self._tasks.append(
                asyncio.create_task(connection.run(), name=f"market-data:{endpoint.name}")
            )
        self._tasks.append(asyncio.create_task(self._watchdog(), name="market-data:watchdog"))
        logger.info(
            "market_data.started",
            markets=len(self._trackers),
            connections=len(self._endpoints),
            depth=self._books is not None,
        )

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._books is not None:
            await self._books.stop()
        for subscriber in self._subscribers:
            subscriber.closed = True
            subscriber.wake.set()
        logger.info("market_data.stopped")

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    # --- consumer API -----------------------------------------------------

    def snapshot(self, ref: MarketRef) -> MarketSnapshot:
        tracker = self._trackers.get(ref)
        if tracker is None:
            raise UnknownMarketError(f"{ref} is not subscribed")
        now = self._clock()
        view = self._books.view(ref) if self._books is not None else DISABLED_VIEW
        ticker = tracker.ticker
        return MarketSnapshot(
            ref=ref,
            status=self._status(tracker, now),
            quote=tracker.quote,
            book=view.book,
            book_status=view.status,
            last_price=ticker.last_price if ticker else None,
            volume_24h=ticker.volume if ticker else None,
            quote_volume_24h=ticker.quote_volume if ticker else None,
            latency_ms=tracker.latency_ms,
            last_update_at=tracker.last_update_at,
            age_ms=(
                _ms_between(now, tracker.last_update_at)
                if tracker.last_update_at is not None
                else None
            ),
            updates=tracker.updates,
            gaps=tracker.gaps,
            resyncs=tracker.resyncs,
            liquidity=view.liquidity,
        )

    def snapshots(self) -> list[MarketSnapshot]:
        return [self.snapshot(ref) for ref in self._trackers]

    def health(self) -> EngineHealth:
        now = self._clock()
        return EngineHealth(
            connections_up=sum(
                state is ConnectionState.CONNECTED for state in self._endpoint_state.values()
            ),
            connections_total=len(self._endpoints),
            markets_live=sum(
                tracker.quote is not None and self._status(tracker, now) is FeedStatus.LIVE
                for tracker in self._trackers.values()
            ),
            markets_total=len(self._trackers),
            books_synced=self._books.synced if self._books is not None else 0,
            books_total=len(self._books) if self._books is not None else 0,
            invalid_messages=self._invalid_messages,
        )

    async def updates(self) -> AsyncIterator[MarketSnapshot]:
        """The latest snapshot of each market as it changes.

        Conflating: a slow consumer receives the newest state of every market
        that changed since it last looked, never a backlog of stale ticks.
        Ends when the engine stops.
        """
        subscriber = _Subscriber()
        self._subscribers.add(subscriber)
        try:
            while not subscriber.closed:
                await subscriber.wake.wait()
                subscriber.wake.clear()
                pending, subscriber.pending = subscriber.pending, {}
                for ref in pending:
                    yield self.snapshot(ref)
        finally:
            self._subscribers.discard(subscriber)

    # --- message path -----------------------------------------------------

    def _handler_for(self, endpoint: StreamEndpoint) -> MessageHandler:
        def handle(raw: str | bytes, received_at: datetime) -> None:
            self._on_message(endpoint, raw, received_at)

        return handle

    def _on_message(
        self, endpoint: StreamEndpoint, raw: str | bytes, received_at: datetime
    ) -> None:
        self._endpoint_heard[endpoint.name] = received_at
        try:
            event = self._source.parse(endpoint, raw, received_at)
        except ExchangeDataError as exc:
            self._invalid_messages += 1
            count = self._invalid_messages
            if count <= 10 or count % _INVALID_LOG_EVERY == 0:
                logger.warning(
                    "market_data.invalid_message",
                    connection=endpoint.name,
                    error=str(exc),
                    total=count,
                )
            return
        if event is None:
            return
        tracker = self._trackers.get(event.ref)
        if tracker is None:
            return  # a market nobody subscribed to

        if isinstance(event, Quote):
            previous = tracker.quote
            if (
                previous is not None
                and previous.sequence is not None
                and event.sequence is not None
                and event.sequence < previous.sequence
            ):
                return  # never replace a newer quote with an older one
            tracker.quote = event
            tracker.touch(event.local_timestamp, event.latency_ms)
        elif isinstance(event, TickerStats):
            tracker.ticker = event
            tracker.touch(event.local_timestamp, event.latency_ms)
        else:
            tracker.touch(event.local_timestamp, event.latency_ms)
            if self._books is not None:
                self._books.apply(event)
        self._notify(event.ref)

    # --- order-book callbacks ---------------------------------------------

    def _book_invalidated(self, ref: MarketRef, reason: str, integrity: bool) -> None:
        if integrity:
            self._trackers[ref].gaps += 1
            self._emit(
                SystemEventType.DATA_GAP,
                Severity.WARNING,
                f"order book invalidated: {reason}",
                market=str(ref),
            )
        else:
            logger.info("market_data.book_rebuild", market=str(ref), reason=reason)
        self._notify(ref)

    def _book_synced(self, ref: MarketRef) -> None:
        self._trackers[ref].resyncs += 1
        self._notify(ref)

    def _snapshot_failed(self, ref: MarketRef, error: ExchangeError) -> None:
        self._emit(
            SystemEventType.API_ERROR,
            Severity.WARNING,
            f"order book snapshot for {ref} failed: {error}",
            market=str(ref),
        )

    # --- connection state and staleness ----------------------------------

    def _on_connection_event(self, event: ConnectionEvent) -> None:
        self._endpoint_state[event.name] = event.state
        endpoint = self._endpoint_by_name[event.name]
        if event.state is ConnectionState.CONNECTED:
            self._endpoint_heard[event.name] = event.at
            for ref in endpoint.refs:
                self._trackers[ref].connected_at = event.at
            if event.connection_number > 1:
                self._emit(
                    SystemEventType.WS_RECONNECTED,
                    Severity.INFO,
                    f"{event.name} reconnected",
                    connection=event.name,
                )
            else:
                self._emit(
                    SystemEventType.WS_CONNECTED,
                    Severity.INFO,
                    f"{event.name} connected",
                    connection=event.name,
                    markets=len(endpoint.refs),
                )
        elif event.state is ConnectionState.DISCONNECTED:
            # Updates sent while we were away are lost, so any book fed by this
            # connection is unknown until rebuilt.
            if self._books is not None:
                for ref, kind in endpoint.streams:
                    if kind is StreamKind.DEPTH:
                        self._books.invalidate(ref, "depth stream disconnected")
            self._emit(
                SystemEventType.WS_DISCONNECTED,
                Severity.WARNING,
                f"{event.name} disconnected: {event.reason}",
                connection=event.name,
                retry_in_seconds=round(event.retry_in_seconds or 0.0, 2),
            )
        else:
            return
        for ref in endpoint.refs:
            self._notify(ref)

    def _is_connected(self, ref: MarketRef) -> bool:
        return all(
            self._endpoint_state[name] is ConnectionState.CONNECTED
            for name in self._ref_endpoints[ref]
        )

    def _status(self, tracker: _MarketTracker, now: datetime) -> FeedStatus:
        if not self._is_connected(tracker.ref):
            if tracker.last_update_at is None:
                return FeedStatus.CONNECTING
            return FeedStatus.DISCONNECTED
        # A connection that has gone quiet can vouch for nothing it carries.
        for name in self._ref_endpoints[tracker.ref]:
            heard = self._endpoint_heard[name]
            if heard is None or _ms_between(now, heard) > self._config.stale_after_ms:
                return FeedStatus.STALE
        # On a live connection a quiet market is unchanged, not stale - the venue
        # pushes every change - but a market silent this long may have lost its
        # own stream. Measured from the later of its last message and the last
        # (re)connect, so a fresh connection gets a full window.
        marks = [mark for mark in (tracker.last_update_at, tracker.connected_at) if mark]
        if marks and _ms_between(now, max(marks)) > self._config.market_silence_ms:
            return FeedStatus.STALE
        return FeedStatus.CONNECTING if tracker.last_update_at is None else FeedStatus.LIVE

    async def _watchdog(self) -> None:
        interval = self._config.stale_check_interval_ms / 1000
        while True:
            await asyncio.sleep(interval)
            now = self._clock()
            for tracker in self._trackers.values():
                status = self._status(tracker, now)
                if status is FeedStatus.STALE and not tracker.stale:
                    tracker.stale = True
                    self._emit(
                        SystemEventType.STALE_DATA,
                        Severity.WARNING,
                        f"{tracker.ref}: no data for over {self._config.stale_after_ms} ms",
                        market=str(tracker.ref),
                    )
                    self._notify(tracker.ref)
                elif status is FeedStatus.LIVE and tracker.stale:
                    tracker.stale = False
                    self._emit(
                        SystemEventType.STALE_DATA,
                        Severity.INFO,
                        f"{tracker.ref}: data flowing again",
                        market=str(tracker.ref),
                    )
                    self._notify(tracker.ref)

    # --- fan-out ------------------------------------------------------------

    def _notify(self, ref: MarketRef) -> None:
        for subscriber in self._subscribers:
            subscriber.pending[ref] = None
            subscriber.wake.set()

    def _emit(
        self, event_type: SystemEventType, severity: Severity, message: str, **context: Any
    ) -> None:
        event = MarketDataEvent(
            event_type=event_type,
            severity=severity,
            message=message,
            occurred_at=self._clock(),
            context=context,
        )
        log = logger.info if severity is Severity.INFO else logger.warning
        log("market_data.event", kind=event_type.value, detail=message, **context)
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:
                logger.exception("market_data.listener_failed", kind=event_type.value)
