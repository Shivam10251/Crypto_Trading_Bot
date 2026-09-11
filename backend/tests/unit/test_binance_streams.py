"""Binance stream routing and message parsing, against recorded live messages.

The ws_*.json fixtures were captured from the live streams on 2026-09-11.
Parsing them, rather than hand-written payloads, pins down the asymmetries that
matter: spot quotes carry no clock, futures depth chains on ``pu``, and futures
volume lives on a different route from futures quotes.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import orjson
import pytest

from trading_bot.core.config import ExchangeConfig
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.binance import BinanceExchangeAdapter
from trading_bot.exchange.binance.streams import BinanceStreamSource, stream_name
from trading_bot.exchange.errors import ExchangeDataError, NotSupportedError
from trading_bot.exchange.models import (
    DepthDiff,
    MarketDataSubscription,
    MarketRef,
    Quote,
    TickerStats,
)
from trading_bot.exchange.streaming import StreamEndpoint, StreamEvent, StreamKind

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "binance"
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)
RECEIVED = datetime(2026, 9, 11, 7, 40, tzinfo=UTC)


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def event_time(message: dict[str, Any]) -> datetime:
    return datetime.fromtimestamp(message["data"]["E"] / 1000, tz=UTC)


def endpoint(ref: MarketRef) -> StreamEndpoint:
    return StreamEndpoint(
        name="test",
        url="wss://test",
        market_type=ref.market_type,
        streams=((ref, StreamKind.QUOTE),),
    )


def parse(
    source: BinanceStreamSource,
    ref: MarketRef,
    message: dict[str, Any],
    received: datetime = RECEIVED,
) -> StreamEvent | None:
    return source.parse(endpoint(ref), orjson.dumps(message), received)


@pytest.fixture
def source() -> BinanceStreamSource:
    return BinanceStreamSource()


class TestEndpoints:
    def test_stream_names_are_lower_case(self) -> None:
        assert stream_name("BTCUSDT", StreamKind.DEPTH) == "btcusdt@depth@100ms"

    def test_spot_streams_share_one_connection(self, source: BinanceStreamSource) -> None:
        subscription = MarketDataSubscription(refs=(SPOT,), include_depth=True, include_ticker=True)
        [only] = source.endpoints(subscription)
        assert only.name == "binance-spot-0"
        assert only.url == (
            "wss://stream.binance.com:9443/stream"
            "?streams=btcusdt@bookTicker/btcusdt@depth@100ms/btcusdt@ticker"
        )
        assert only.refs == (SPOT,)

    def test_futures_quotes_and_volume_use_different_routes(
        self, source: BinanceStreamSource
    ) -> None:
        """Verified live: @ticker on the quote route connects and then says nothing."""
        subscription = MarketDataSubscription(refs=(PERP,), include_depth=True, include_ticker=True)
        by_name = {e.name: e for e in source.endpoints(subscription)}
        assert set(by_name) == {"binance-perpetual-public-0", "binance-perpetual-market-0"}
        assert by_name["binance-perpetual-public-0"].url == (
            "wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker/btcusdt@depth@100ms"
        )
        assert by_name["binance-perpetual-market-0"].url == (
            "wss://fstream.binance.com/market/stream?streams=btcusdt@ticker"
        )

    def test_top_of_book_needs_only_quote_streams(self, source: BinanceStreamSource) -> None:
        urls = [e.url for e in source.endpoints(MarketDataSubscription.top_of_book(SPOT, PERP))]
        assert urls == [
            "wss://stream.binance.com:9443/stream?streams=btcusdt@bookTicker",
            "wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker",
        ]

    def test_connections_split_at_the_stream_limit(self) -> None:
        source = BinanceStreamSource(max_streams_per_connection=2)
        refs = tuple(
            MarketRef("binance", symbol, MarketType.SPOT)
            for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")
        )
        endpoints = source.endpoints(MarketDataSubscription.top_of_book(*refs))
        assert [e.name for e in endpoints] == ["binance-spot-0", "binance-spot-1"]
        assert [len(e.streams) for e in endpoints] == [2, 1]
        assert endpoints[1].refs == (refs[2],)

    def test_duplicate_markets_are_subscribed_once(self, source: BinanceStreamSource) -> None:
        [only] = source.endpoints(MarketDataSubscription.top_of_book(SPOT, SPOT))
        assert only.url.count("btcusdt@bookTicker") == 1

    def test_markets_of_another_venue_are_rejected(self, source: BinanceStreamSource) -> None:
        foreign = MarketRef("kraken", "XBTUSD", MarketType.SPOT)
        with pytest.raises(ValueError, match="not a binance market"):
            source.endpoints(MarketDataSubscription.top_of_book(foreign))

    def test_trade_streams_are_not_offered_yet(self, source: BinanceStreamSource) -> None:
        with pytest.raises(NotSupportedError, match="trade streams"):
            source.endpoints(MarketDataSubscription(refs=(SPOT,), include_trades=True))

    def test_delivery_futures_are_not_streamed(self, source: BinanceStreamSource) -> None:
        delivery = MarketRef("binance", "BTCUSDT_261225", MarketType.FUTURE)
        with pytest.raises(NotSupportedError):
            source.endpoints(MarketDataSubscription.top_of_book(delivery))

    @pytest.mark.parametrize("limit", [0, 201])
    def test_stream_limit_is_validated(self, limit: int) -> None:
        with pytest.raises(ValueError, match="max_streams_per_connection"):
            BinanceStreamSource(max_streams_per_connection=limit)

    async def test_adapter_takes_hosts_from_configuration(self) -> None:
        config = ExchangeConfig(
            spot_ws_url="wss://spot.example.test/", futures_ws_url="wss://futures.example.test"
        )
        async with BinanceExchangeAdapter(config) as adapter:
            subscription = MarketDataSubscription.top_of_book(SPOT, PERP)
            urls = [e.url for e in adapter.stream_source().endpoints(subscription)]
        assert urls == [
            "wss://spot.example.test/stream?streams=btcusdt@bookTicker",
            "wss://futures.example.test/public/stream?streams=btcusdt@bookTicker",
        ]


class TestQuotes:
    def test_spot_quote_has_no_exchange_clock(self, source: BinanceStreamSource) -> None:
        message = fixture("ws_spot_book_ticker")[0]
        quote = parse(source, SPOT, message)
        assert isinstance(quote, Quote)
        assert quote.ref == SPOT
        assert quote.bid == Decimal(message["data"]["b"])
        assert quote.ask_size == Decimal(message["data"]["A"])
        assert quote.sequence == message["data"]["u"]
        assert quote.local_timestamp == RECEIVED
        assert quote.exchange_timestamp is None
        assert quote.latency_ms is None

    def test_futures_quote_carries_event_time(self, source: BinanceStreamSource) -> None:
        message = fixture("ws_futures_book_ticker")[0]
        quote = parse(source, PERP, message, event_time(message) + timedelta(milliseconds=42))
        assert isinstance(quote, Quote)
        assert quote.exchange_timestamp == event_time(message)
        assert quote.latency_ms == 42

    def test_text_frames_parse_like_binary_ones(self, source: BinanceStreamSource) -> None:
        message = fixture("ws_spot_book_ticker")[0]
        as_text = source.parse(endpoint(SPOT), json.dumps(message), RECEIVED)
        assert as_text == parse(source, SPOT, message)


class TestDepth:
    def test_spot_update_has_no_previous_id(self, source: BinanceStreamSource) -> None:
        message = fixture("ws_spot_depth_sequence")["diffs"][0]
        diff = parse(source, SPOT, message)
        assert isinstance(diff, DepthDiff)
        assert diff.first_update_id == message["data"]["U"]
        assert diff.final_update_id == message["data"]["u"]
        assert diff.previous_final_update_id is None
        assert len(diff.bids) == len(message["data"]["b"])
        assert diff.exchange_timestamp == event_time(message)

    def test_futures_update_chains_to_its_predecessor(self, source: BinanceStreamSource) -> None:
        message = fixture("ws_futures_depth_sequence")["diffs"][0]
        diff = parse(source, PERP, message)
        assert isinstance(diff, DepthDiff)
        assert diff.previous_final_update_id == message["data"]["pu"]

    def test_zero_size_is_kept_because_it_deletes_a_level(
        self, source: BinanceStreamSource
    ) -> None:
        message = copy.deepcopy(fixture("ws_spot_depth_sequence")["diffs"][0])
        message["data"]["b"][0][1] = "0.00000000"
        diff = parse(source, SPOT, message)
        assert isinstance(diff, DepthDiff)
        assert diff.bids[0].size == 0

    def test_reversed_update_ids_are_rejected(self, source: BinanceStreamSource) -> None:
        message = copy.deepcopy(fixture("ws_spot_depth_sequence")["diffs"][0])
        data = message["data"]
        data["U"], data["u"] = data["u"], data["U"]
        with pytest.raises(ExchangeDataError, match="reversed"):
            parse(source, SPOT, message)


class TestTicker:
    @pytest.mark.parametrize(
        ("name", "ref"), [("ws_spot_ticker", SPOT), ("ws_futures_ticker", PERP)]
    )
    def test_volume_and_last_price(
        self, source: BinanceStreamSource, name: str, ref: MarketRef
    ) -> None:
        message = fixture(name)[0]
        stats = parse(source, ref, message)
        assert isinstance(stats, TickerStats)
        assert stats.volume == Decimal(message["data"]["v"])
        assert stats.quote_volume == Decimal(message["data"]["q"])
        assert stats.last_price == Decimal(message["data"]["c"])
        assert stats.exchange_timestamp == event_time(message)


class TestValidation:
    def test_control_replies_carry_no_data(self, source: BinanceStreamSource) -> None:
        assert source.parse(endpoint(SPOT), b'{"result": null, "id": 1}', RECEIVED) is None

    @pytest.mark.parametrize(
        "raw",
        [
            b"not json",
            b"[1, 2]",
            b'{"error": {"code": 2, "msg": "Invalid request"}}',
            b'{"stream": "btcusdt@bookTicker", "data": [1]}',
            b'{"stream": "btcusdt@kline_1m", "data": {"s": "BTCUSDT"}}',
        ],
        ids=["not-json", "not-an-object", "error-frame", "bad-envelope", "unexpected-stream"],
    )
    def test_malformed_frames_are_rejected(self, source: BinanceStreamSource, raw: bytes) -> None:
        with pytest.raises(ExchangeDataError):
            source.parse(endpoint(SPOT), raw, RECEIVED)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("s", "ETHUSDT"),
            ("b", "abc"),
            ("b", "NaN"),
            ("b", "99999999"),
            ("u", True),
            ("u", "1"),
            ("a", None),
        ],
        ids=["wrong-symbol", "unparseable", "nan", "crossed", "bool-id", "string-id", "missing"],
    )
    def test_invalid_quotes_never_reach_a_consumer(
        self, source: BinanceStreamSource, key: str, value: object
    ) -> None:
        message = copy.deepcopy(fixture("ws_spot_book_ticker")[0])
        if value is None:
            del message["data"][key]
        else:
            message["data"][key] = value
        with pytest.raises(ExchangeDataError):
            parse(source, SPOT, message)
