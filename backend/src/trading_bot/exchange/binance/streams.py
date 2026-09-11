"""Binance WebSocket streams: which URLs to open and how to read what arrives.

Message shapes verified against the live API on 2026-09-11 (combined-stream
envelope ``{"stream": ..., "data": ...}``); recordings live in
``tests/fixtures/binance/ws_*.json``:

  spot    <s>@bookTicker   {u,s,b,B,a,A}             no clock
  futures <s>@bookTicker   {e,u,s,b,B,a,A,T,E}       has clock
  spot    <s>@depth@100ms  {e,E,s,U,u,b,a}           next U == previous u + 1
  futures <s>@depth@100ms  {e,E,T,s,U,u,pu,b,a}      next pu == previous u
  both    <s>@ticker       {e,E,s,c,v,q,...}         rolling 24h

Spot quotes carry no exchange timestamp even when streamed, so spot latency is
measured from depth and ticker events, which do. A quote is never given a clock
it did not arrive with.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import orjson

from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.binance.endpoints import (
    FUTURES_WS_BASE,
    MAX_STREAMS_PER_CONNECTION,
    SPOT_WS_BASE,
    ws_path,
)
from trading_bot.exchange.binance.mapping import to_datetime, to_decimal
from trading_bot.exchange.errors import ExchangeDataError, NotSupportedError
from trading_bot.exchange.models import (
    BookLevel,
    DepthDiff,
    MarketDataSubscription,
    MarketRef,
    Quote,
    TickerStats,
)
from trading_bot.exchange.streaming import (
    MarketStreamSource,
    StreamEndpoint,
    StreamEvent,
    StreamKind,
)

# 100 ms is the fastest diff-depth cadence both venues offer.
_SUFFIXES: dict[StreamKind, str] = {
    StreamKind.QUOTE: "@bookTicker",
    StreamKind.DEPTH: "@depth@100ms",
    StreamKind.TICKER: "@ticker",
}


def stream_name(symbol: str, kind: StreamKind) -> str:
    """Binance stream names are lower-case: ``btcusdt@bookTicker``."""
    return f"{symbol.lower()}{_SUFFIXES[kind]}"


def _kind_of(stream: str) -> StreamKind | None:
    for kind, suffix in _SUFFIXES.items():
        if stream.endswith(suffix):
            return kind
    return None


class BinanceStreamSource(MarketStreamSource):
    """Stream routing and message parsing for binance.com spot and USD-M."""

    def __init__(
        self,
        *,
        venue: str = "binance",
        spot_ws_base: str = SPOT_WS_BASE,
        futures_ws_base: str = FUTURES_WS_BASE,
        max_streams_per_connection: int = MAX_STREAMS_PER_CONNECTION,
    ) -> None:
        if not 1 <= max_streams_per_connection <= MAX_STREAMS_PER_CONNECTION:
            raise ValueError(
                f"max_streams_per_connection must be within 1..{MAX_STREAMS_PER_CONNECTION}"
            )
        self.venue = venue
        self._bases = {
            MarketType.SPOT: spot_ws_base.rstrip("/"),
            MarketType.PERPETUAL: futures_ws_base.rstrip("/"),
        }
        self._max_streams = max_streams_per_connection

    def endpoints(self, subscription: MarketDataSubscription) -> list[StreamEndpoint]:
        if subscription.include_trades:
            raise NotSupportedError(
                "trade streams are not consumed yet; volume comes from the 24h ticker"
            )
        kinds = [StreamKind.QUOTE]
        if subscription.include_depth:
            kinds.append(StreamKind.DEPTH)
        if subscription.include_ticker:
            kinds.append(StreamKind.TICKER)

        # Group by (instrument class, route): each group becomes one or more
        # connections, split at the per-connection stream limit.
        groups: dict[tuple[MarketType, str], list[tuple[MarketRef, StreamKind]]] = {}
        for ref in dict.fromkeys(subscription.refs):
            if ref.venue != self.venue:
                raise ValueError(f"{ref} is not a {self.venue} market")
            if ref.market_type not in self._bases:
                raise NotSupportedError(f"{ref.market_type.value} markets are not streamed")
            for kind in kinds:
                key = (ref.market_type, ws_path(ref.market_type, kind))
                groups.setdefault(key, []).append((ref, kind))

        endpoints: list[StreamEndpoint] = []
        for (market_type, path), streams in groups.items():
            route = path.removesuffix("/stream").strip("/")
            for index, start in enumerate(range(0, len(streams), self._max_streams)):
                chunk = tuple(streams[start : start + self._max_streams])
                names = "/".join(stream_name(ref.symbol, kind) for ref, kind in chunk)
                parts = (self.venue, market_type.value.lower(), route, str(index))
                endpoints.append(
                    StreamEndpoint(
                        name="-".join(part for part in parts if part),
                        url=f"{self._bases[market_type]}{path}?streams={names}",
                        market_type=market_type,
                        streams=chunk,
                    )
                )
        return endpoints

    def parse(
        self, endpoint: StreamEndpoint, raw: str | bytes, received_at: datetime
    ) -> StreamEvent | None:
        try:
            message = orjson.loads(raw)
        except orjson.JSONDecodeError as exc:
            raise ExchangeDataError(f"{endpoint.name}: message is not JSON") from exc
        if not isinstance(message, dict):
            raise ExchangeDataError(f"{endpoint.name}: expected a JSON object")
        if "stream" not in message:
            # Replies to control requests ({"result": null, "id": 1}) carry no
            # stream. Error frames do not either, and must not pass silently.
            if "error" in message:
                raise ExchangeDataError(f"{endpoint.name}: venue error {message['error']}")
            return None

        stream, data = message["stream"], message.get("data")
        if not isinstance(stream, str) or not isinstance(data, dict):
            raise ExchangeDataError(f"{endpoint.name}: malformed stream envelope")
        kind = _kind_of(stream)
        if kind is None:
            raise ExchangeDataError(f"{endpoint.name}: unexpected stream {stream!r}")
        symbol = data.get("s")
        if not isinstance(symbol, str) or stream_name(symbol, kind) != stream:
            raise ExchangeDataError(
                f"{endpoint.name}: symbol {symbol!r} does not match stream {stream!r}"
            )

        ref = MarketRef(venue=self.venue, symbol=symbol.upper(), market_type=endpoint.market_type)
        if kind is StreamKind.QUOTE:
            return parse_ws_quote(data, ref, received_at)
        if kind is StreamKind.DEPTH:
            return parse_ws_depth(data, ref, received_at)
        return parse_ws_ticker(data, ref, received_at)


def _field(data: dict[str, Any], key: str, context: str) -> Any:
    value = data.get(key)
    if value is None:
        raise ExchangeDataError(f"{context}: missing field {key!r}")
    return value


def _number(data: dict[str, Any], key: str, context: str) -> Decimal:
    return to_decimal(_field(data, key, context), f"{context} {key}")


def _update_id(data: dict[str, Any], key: str, context: str) -> int:
    raw = _field(data, key, context)
    # bool is an int subclass; a boolean here is a malformed payload.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ExchangeDataError(f"{context}: {key} is not an integer update id")
    return raw


def parse_ws_quote(data: dict[str, Any], ref: MarketRef, local_timestamp: datetime) -> Quote:
    """``bookTicker`` stream. ``E`` exists on futures only."""
    context = f"bookTicker stream {ref}"
    event_time = data.get("E")
    return Quote(
        ref=ref,
        bid=_number(data, "b", context),
        ask=_number(data, "a", context),
        bid_size=_number(data, "B", context),
        ask_size=_number(data, "A", context),
        local_timestamp=local_timestamp,
        exchange_timestamp=to_datetime(event_time, context) if event_time is not None else None,
        sequence=_update_id(data, "u", context),
    )


def parse_ws_depth(data: dict[str, Any], ref: MarketRef, local_timestamp: datetime) -> DepthDiff:
    """``depth@100ms`` stream. ``pu`` exists on futures only."""
    context = f"depth stream {ref}"
    previous = data.get("pu")
    return DepthDiff(
        ref=ref,
        first_update_id=_update_id(data, "U", context),
        final_update_id=_update_id(data, "u", context),
        previous_final_update_id=(
            _update_id(data, "pu", context) if previous is not None else None
        ),
        bids=_diff_levels(_field(data, "b", context), f"{context} bids"),
        asks=_diff_levels(_field(data, "a", context), f"{context} asks"),
        local_timestamp=local_timestamp,
        exchange_timestamp=to_datetime(_field(data, "E", context), context),
    )


def _diff_levels(raw: Any, context: str) -> tuple[BookLevel, ...]:
    """Unlike a snapshot, size zero is meaningful here: it deletes the level."""
    if not isinstance(raw, list):
        raise ExchangeDataError(f"{context}: expected a list of levels")
    levels: list[BookLevel] = []
    for entry in raw:
        if not isinstance(entry, list) or len(entry) < 2:
            raise ExchangeDataError(f"{context}: malformed level {entry!r}")
        levels.append(
            BookLevel(
                price=to_decimal(entry[0], f"{context} price"),
                size=to_decimal(entry[1], f"{context} size"),
            )
        )
    return tuple(levels)


def parse_ws_ticker(data: dict[str, Any], ref: MarketRef, local_timestamp: datetime) -> TickerStats:
    """``ticker`` stream: rolling 24h last price and volume."""
    context = f"ticker stream {ref}"
    return TickerStats(
        ref=ref,
        last_price=_number(data, "c", context),
        volume=_number(data, "v", context),
        quote_volume=_number(data, "q", context),
        exchange_timestamp=to_datetime(_field(data, "E", context), context),
        local_timestamp=local_timestamp,
    )
