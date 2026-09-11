"""The market-data engine end to end, against a fake venue.

Messages are recorded live Binance payloads - or synthetic ones in the same
shape where a test needs a particular sequence - fed through the real parser,
the real connection and the real book synchronisation. Only the socket and the
REST snapshot are fakes.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import orjson
import pytest

from trading_bot.core.config import MarketDataConfig
from trading_bot.db.models.enums import MarketType, Severity, SystemEventType
from trading_bot.exchange.binance.mapping import parse_order_book
from trading_bot.exchange.binance.streams import BinanceStreamSource
from trading_bot.exchange.errors import UnknownMarketError
from trading_bot.exchange.models import (
    BookLevel,
    MarketDataSubscription,
    MarketRef,
    OrderBook,
    Quote,
)
from trading_bot.exchange.streaming import (
    MarketStreamSource,
    StreamEndpoint,
    StreamEvent,
    StreamKind,
)
from trading_bot.marketdata import BookStatus, FeedStatus, MarketDataEngine, MarketDataEvent

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "binance"
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)
START = datetime(2026, 9, 11, 7, 40, tzinfo=UTC)


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def fast_config(**overrides: Any) -> MarketDataConfig:
    values: dict[str, Any] = {
        "stale_after_ms": 2000,
        "stale_check_interval_ms": 5,
        "reconnect_initial_backoff_seconds": 0.001,
        "resync_min_interval_seconds": 0,
        "idle_timeout_seconds": 30,
    }
    return MarketDataConfig(**{**values, **overrides})


def millis(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def book_ticker(ref: MarketRef, update_id: int, bid: str, ask: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "u": update_id,
        "s": ref.symbol,
        "b": bid,
        "B": "1.5",
        "a": ask,
        "A": "2",
    }
    return {"stream": f"{ref.symbol.lower()}@bookTicker", "data": data}


def depth_update(
    ref: MarketRef,
    first: int,
    final: int,
    *,
    bids: tuple[tuple[str, str], ...] = (),
    asks: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    data = {
        "e": "depthUpdate",
        "E": millis(START),
        "s": ref.symbol,
        "U": first,
        "u": final,
        "b": [list(level) for level in bids],
        "a": [list(level) for level in asks],
    }
    return {"stream": f"{ref.symbol.lower()}@depth@100ms", "data": data}


def ladder(ref: MarketRef, sequence: int) -> OrderBook:
    """Bids 99..95 and asks 101..105, one unit each."""
    return OrderBook(
        ref=ref,
        bids=tuple(BookLevel(Decimal(100 - i), Decimal(1)) for i in range(1, 6)),
        asks=tuple(BookLevel(Decimal(100 + i), Decimal(1)) for i in range(1, 6)),
        local_timestamp=START,
        sequence=sequence,
    )


def recorded_sequence(ref: MarketRef) -> tuple[OrderBook, list[dict[str, Any]]]:
    spot = ref.market_type is MarketType.SPOT
    data = fixture("ws_spot_depth_sequence" if spot else "ws_futures_depth_sequence")
    return parse_order_book(data["snapshot"], ref, local_timestamp=START), data["diffs"]


async def eventually(predicate: Callable[[], bool], within: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


class Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += timedelta(milliseconds=ms)


class QueueSocket:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | BaseException] = asyncio.Queue()

    async def recv(self) -> bytes:
        item = await self.queue.get()
        if isinstance(item, BaseException):
            raise item
        return item


class FakeVenue:
    """Stands in for the exchange's WebSocket servers: one socket per route."""

    def __init__(self) -> None:
        self.sockets: dict[str, QueueSocket] = {}
        self.connects: dict[str, int] = defaultdict(int)

    @staticmethod
    def route(url: str) -> str:
        if "/public/" in url:
            return "perp-public"
        if "/market/" in url:
            return "perp-market"
        return "spot"

    def connector(self, url: str) -> AbstractAsyncContextManager[QueueSocket]:
        return self._open(self.route(url))

    @asynccontextmanager
    async def _open(self, route: str) -> AsyncIterator[QueueSocket]:
        socket = QueueSocket()
        self.sockets[route] = socket
        self.connects[route] += 1
        try:
            yield socket
        finally:
            if self.sockets.get(route) is socket:
                del self.sockets[route]

    async def wait_connected(self, *routes: str) -> None:
        await eventually(lambda: all(route in self.sockets for route in routes))

    def send(self, route: str, message: dict[str, Any]) -> None:
        self.send_raw(route, orjson.dumps(message))

    def send_raw(self, route: str, raw: bytes) -> None:
        self.sockets[route].queue.put_nowait(raw)

    def drop(self, route: str) -> None:
        self.sockets[route].queue.put_nowait(OSError("connection reset by peer"))

    async def drained(self, route: str) -> None:
        await eventually(lambda: route in self.sockets and self.sockets[route].queue.empty())
        await asyncio.sleep(0)


@dataclass
class Harness:
    engine: MarketDataEngine
    venue: FakeVenue
    clock: Clock
    events: list[MarketDataEvent]
    fetches: list[tuple[MarketRef, int]]

    def event_types(self) -> list[SystemEventType]:
        return [event.event_type for event in self.events]


@asynccontextmanager
async def running(
    subscription: MarketDataSubscription,
    *,
    snapshots: dict[MarketRef, list[OrderBook]] | None = None,
    config: MarketDataConfig | None = None,
    source: MarketStreamSource | None = None,
    clock: Clock | None = None,
) -> AsyncIterator[Harness]:
    venue = FakeVenue()
    clock = clock or Clock()
    fetches: list[tuple[MarketRef, int]] = []
    queued = {ref: list(books) for ref, books in (snapshots or {}).items()}

    async def fetch(ref: MarketRef, levels: int) -> OrderBook:
        fetches.append((ref, levels))
        books = queued[ref]
        return books.pop(0) if len(books) > 1 else books[0]

    engine = MarketDataEngine(
        source or BinanceStreamSource(),
        subscription,
        config or fast_config(),
        snapshot_fetcher=fetch if subscription.include_depth else None,
        connector=venue.connector,
        clock=clock,
    )
    events: list[MarketDataEvent] = []
    engine.add_listener(events.append)
    async with engine:
        yield Harness(engine, venue, clock, events, fetches)


class TestQuotes:
    async def test_recorded_quotes_become_live_snapshots(self) -> None:
        futures_message = fixture("ws_futures_book_ticker")[0]
        spot_message = fixture("ws_spot_book_ticker")[0]
        exchange_time = datetime.fromtimestamp(futures_message["data"]["E"] / 1000, tz=UTC)
        clock = Clock(exchange_time + timedelta(milliseconds=40))
        subscription = MarketDataSubscription.top_of_book(SPOT, PERP)
        async with running(subscription, clock=clock) as h:
            await h.venue.wait_connected("spot", "perp-public")
            h.venue.send("spot", spot_message)
            h.venue.send("perp-public", futures_message)
            await eventually(lambda: all(h.engine.snapshot(r).is_live for r in (SPOT, PERP)))
            spot, perp = h.engine.snapshot(SPOT), h.engine.snapshot(PERP)
            health = h.engine.health()
        assert spot.best_bid == Decimal(spot_message["data"]["b"])
        assert spot.exchange_timestamp is None
        assert spot.latency_ms is None  # the quote has no clock and nothing else arrived
        assert perp.latency_ms == 40
        assert perp.book_status is BookStatus.DISABLED
        assert (health.connections_up, health.markets_live) == (2, 2)

    async def test_ticker_supplies_volume_and_a_spot_latency(self) -> None:
        message = fixture("ws_spot_ticker")[0]
        event_time = datetime.fromtimestamp(message["data"]["E"] / 1000, tz=UTC)
        subscription = MarketDataSubscription(refs=(SPOT,), include_ticker=True)
        clock = Clock(event_time + timedelta(milliseconds=25))
        async with running(subscription, clock=clock) as h:
            await h.venue.wait_connected("spot")
            h.venue.send("spot", message)
            await eventually(lambda: h.engine.snapshot(SPOT).volume_24h is not None)
            snap = h.engine.snapshot(SPOT)
        assert snap.volume_24h == Decimal(message["data"]["v"])
        assert snap.quote_volume_24h == Decimal(message["data"]["q"])
        assert snap.last_price == Decimal(message["data"]["c"])
        assert snap.latency_ms == 25
        assert not snap.is_live  # data, but no quote yet: nothing to act on

    async def test_futures_volume_arrives_on_the_market_route(self) -> None:
        subscription = MarketDataSubscription(refs=(PERP,), include_ticker=True)
        async with running(subscription) as h:
            await h.venue.wait_connected("perp-public", "perp-market")
            h.venue.send("perp-market", fixture("ws_futures_ticker")[0])
            await eventually(lambda: h.engine.snapshot(PERP).volume_24h is not None)

    async def test_an_older_quote_never_replaces_a_newer_one(self) -> None:
        async with running(MarketDataSubscription.top_of_book(SPOT)) as h:
            await h.venue.wait_connected("spot")
            h.venue.send("spot", book_ticker(SPOT, 10, "100.1", "100.2"))
            h.venue.send("spot", book_ticker(SPOT, 9, "99.0", "99.1"))
            await h.venue.drained("spot")
            quote = h.engine.snapshot(SPOT).quote
        assert quote is not None
        assert (quote.sequence, quote.bid) == (10, Decimal("100.1"))

    async def test_invalid_messages_are_counted_not_fatal(self) -> None:
        async with running(MarketDataSubscription.top_of_book(SPOT)) as h:
            await h.venue.wait_connected("spot")
            h.venue.send_raw("spot", b"{not json")
            h.venue.send("spot", book_ticker(SPOT, 1, "abc", "100"))
            h.venue.send("spot", book_ticker(SPOT, 2, "100", "101"))
            await eventually(lambda: h.engine.snapshot(SPOT).is_live)
            assert h.engine.health().invalid_messages == 2
        assert h.venue.connects["spot"] == 1  # the connection survived


class TestOrderBooks:
    @pytest.mark.parametrize(("ref", "route"), [(SPOT, "spot"), (PERP, "perp-public")])
    async def test_recorded_sequence_synchronises(self, ref: MarketRef, route: str) -> None:
        snapshot, diffs = recorded_sequence(ref)
        last = diffs[-1]["data"]["u"]
        subscription = MarketDataSubscription(refs=(ref,), include_depth=True, depth_levels=20)
        async with running(subscription, snapshots={ref: [snapshot]}) as h:
            await h.venue.wait_connected(route)
            for message in diffs:
                h.venue.send(route, message)
            await eventually(
                lambda: (book := h.engine.snapshot(ref).book) is not None and book.sequence == last
            )
            snap = h.engine.snapshot(ref)
        assert h.fetches == [(ref, fast_config().snapshot_depth)]
        assert (snap.resyncs, snap.gaps) == (1, 0)
        assert snap.book is not None
        assert len(snap.book.bids) == len(snap.book.asks) == 20
        assert snap.book.best_bid < snap.book.best_ask

    async def test_a_sequence_gap_rebuilds_the_book(self) -> None:
        subscription = MarketDataSubscription(refs=(SPOT,), include_depth=True, depth_levels=2)
        snapshots = {SPOT: [ladder(SPOT, 100), ladder(SPOT, 200)]}
        async with running(subscription, snapshots=snapshots) as h:
            await h.venue.wait_connected("spot")
            h.venue.send("spot", depth_update(SPOT, 101, 101, bids=(("99", "2"),)))
            await eventually(lambda: h.engine.snapshot(SPOT).book_status is BookStatus.SYNCED)
            h.venue.send("spot", depth_update(SPOT, 102, 102))
            h.venue.send("spot", depth_update(SPOT, 105, 106))  # 103-104 never arrived
            await eventually(lambda: SystemEventType.DATA_GAP in h.event_types())
            await eventually(
                lambda: (book := h.engine.snapshot(SPOT).book) is not None and book.sequence == 200
            )
            h.venue.send("spot", depth_update(SPOT, 201, 201, asks=(("101", "3"),)))
            await eventually(
                lambda: (book := h.engine.snapshot(SPOT).book) is not None and book.sequence == 201
            )
            snap = h.engine.snapshot(SPOT)
        assert (snap.gaps, snap.resyncs) == (1, 2)
        assert len(h.fetches) == 2
        assert snap.book is not None
        assert snap.book.asks[0] == BookLevel(Decimal(101), Decimal(3))
        gap = next(e for e in h.events if e.event_type is SystemEventType.DATA_GAP)
        assert gap.severity is Severity.WARNING
        assert gap.context["market"] == str(SPOT)

    async def test_nothing_is_trusted_across_a_disconnect(self) -> None:
        subscription = MarketDataSubscription(refs=(SPOT,), include_depth=True, depth_levels=2)
        snapshots = {SPOT: [ladder(SPOT, 100), ladder(SPOT, 300)]}
        async with running(subscription, snapshots=snapshots) as h:
            await h.venue.wait_connected("spot")
            h.venue.send("spot", depth_update(SPOT, 101, 101))
            await eventually(lambda: h.engine.snapshot(SPOT).book_status is BookStatus.SYNCED)
            h.venue.drop("spot")
            await eventually(lambda: SystemEventType.WS_RECONNECTED in h.event_types())
            assert h.engine.snapshot(SPOT).book_status is BookStatus.SYNCING
            h.venue.send("spot", depth_update(SPOT, 301, 301))
            await eventually(lambda: h.engine.snapshot(SPOT).book_status is BookStatus.SYNCED)
            snap = h.engine.snapshot(SPOT)
        assert h.venue.connects["spot"] == 2
        assert snap.gaps == 0  # a disconnect is not a data-integrity failure
        connection_events = [t for t in h.event_types() if t.name.startswith("WS_")]
        assert connection_events == [
            SystemEventType.WS_CONNECTED,
            SystemEventType.WS_DISCONNECTED,
            SystemEventType.WS_RECONNECTED,
        ]


class TestFreshness:
    async def test_a_market_is_disconnected_while_its_connection_is_down(self) -> None:
        config = fast_config(reconnect_initial_backoff_seconds=60)
        async with running(MarketDataSubscription.top_of_book(SPOT), config=config) as h:
            await h.venue.wait_connected("spot")
            h.venue.send("spot", book_ticker(SPOT, 1, "100", "101"))
            await eventually(lambda: h.engine.snapshot(SPOT).is_live)
            h.venue.drop("spot")
            await eventually(lambda: h.engine.snapshot(SPOT).status is FeedStatus.DISCONNECTED)
            health = h.engine.health()
        assert (health.connections_up, health.markets_live) == (0, 0)
        [disconnect] = [e for e in h.events if e.event_type is SystemEventType.WS_DISCONNECTED]
        assert disconnect.severity is Severity.WARNING
        assert "connection reset by peer" in disconnect.message

    async def test_silence_marks_a_market_stale_until_data_returns(self) -> None:
        async with running(MarketDataSubscription.top_of_book(SPOT)) as h:
            await h.venue.wait_connected("spot")
            h.venue.send("spot", book_ticker(SPOT, 1, "100", "101"))
            await eventually(lambda: h.engine.snapshot(SPOT).is_live)
            h.clock.advance(2500)
            await eventually(lambda: SystemEventType.STALE_DATA in h.event_types())
            assert h.engine.snapshot(SPOT).status is FeedStatus.STALE
            h.venue.send("spot", book_ticker(SPOT, 2, "100", "101"))
            await eventually(lambda: h.event_types().count(SystemEventType.STALE_DATA) == 2)
            assert h.engine.snapshot(SPOT).status is FeedStatus.LIVE
        stale = [e for e in h.events if e.event_type is SystemEventType.STALE_DATA]
        # Reported once on the way down and once on the way up, not on every check.
        assert [e.severity for e in stale] == [Severity.WARNING, Severity.INFO]

    async def test_a_connected_market_that_never_delivers_goes_stale(self) -> None:
        async with running(MarketDataSubscription.top_of_book(SPOT)) as h:
            await h.venue.wait_connected("spot")
            assert h.engine.snapshot(SPOT).status is FeedStatus.CONNECTING
            h.clock.advance(2500)
            assert h.engine.snapshot(SPOT).status is FeedStatus.STALE


class TestConsumers:
    async def test_a_slow_consumer_gets_the_latest_state_not_a_backlog(self) -> None:
        async with running(MarketDataSubscription.top_of_book(SPOT)) as h:
            await h.venue.wait_connected("spot")
            updates = h.engine.updates()
            first = asyncio.ensure_future(anext(updates))
            await asyncio.sleep(0)  # let the consumer subscribe
            h.venue.send("spot", book_ticker(SPOT, 1, "100", "101"))
            assert (await asyncio.wait_for(first, 1)).quote.sequence == 1  # type: ignore[union-attr]
            for update_id in range(2, 52):
                h.venue.send("spot", book_ticker(SPOT, update_id, "100", "101"))
            await h.venue.drained("spot")
            latest = await asyncio.wait_for(anext(updates), 1)
            assert latest.quote is not None
            assert latest.quote.sequence == 51
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(updates), 0.05)
            await updates.aclose()

    async def test_update_iterators_end_when_the_engine_stops(self) -> None:
        async with running(MarketDataSubscription.top_of_book(SPOT)) as h:
            pending = asyncio.ensure_future(anext(h.engine.updates()))
            await asyncio.sleep(0)
        with pytest.raises(StopAsyncIteration):
            await pending

    async def test_the_engine_works_for_any_venue(self) -> None:
        """No Binance anywhere: a made-up venue with a one-line format."""

        class LineSource(MarketStreamSource):
            venue = "line_exchange"

            def endpoints(self, subscription: MarketDataSubscription) -> list[StreamEndpoint]:
                streams = tuple((ref, StreamKind.QUOTE) for ref in subscription.refs)
                return [StreamEndpoint("line-0", "wss://line.test/feed", MarketType.SPOT, streams)]

            def parse(
                self, endpoint: StreamEndpoint, raw: str | bytes, received_at: datetime
            ) -> StreamEvent | None:
                text = raw.decode() if isinstance(raw, bytes) else raw
                symbol, bid, ask, update_id = text.split()
                return Quote(
                    ref=MarketRef(self.venue, symbol, MarketType.SPOT),
                    bid=Decimal(bid),
                    ask=Decimal(ask),
                    bid_size=Decimal(1),
                    ask_size=Decimal(1),
                    local_timestamp=received_at,
                    sequence=int(update_id),
                )

        ref = MarketRef("line_exchange", "XYZUSD", MarketType.SPOT)
        subscription = MarketDataSubscription.top_of_book(ref)
        async with running(subscription, source=LineSource()) as h:
            await h.venue.wait_connected("spot")
            h.venue.send_raw("spot", b"XYZUSD 10 11 1")
            await eventually(lambda: h.engine.snapshot(ref).is_live)
            assert h.engine.snapshot(ref).mid_price == Decimal("10.5")


class TestConstruction:
    def test_depth_needs_a_snapshot_fetcher(self) -> None:
        subscription = MarketDataSubscription(refs=(SPOT,), include_depth=True)
        with pytest.raises(ValueError, match="snapshot fetcher"):
            MarketDataEngine(BinanceStreamSource(), subscription, fast_config())

    def test_unsubscribed_markets_are_unknown(self) -> None:
        engine = MarketDataEngine(
            BinanceStreamSource(), MarketDataSubscription.top_of_book(SPOT), fast_config()
        )
        with pytest.raises(UnknownMarketError):
            engine.snapshot(PERP)

    async def test_starting_twice_is_refused(self) -> None:
        async with running(MarketDataSubscription.top_of_book(SPOT)) as h:
            with pytest.raises(RuntimeError, match="already started"):
                await h.engine.start()
