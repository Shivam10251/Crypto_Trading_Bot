"""binance.com endpoint map.

Spot and USD-M futures are separate hosts with different path prefixes and,
importantly, different payload shapes - futures report an event time, spot does
not. Keeping the routing in one table makes that asymmetry visible.

Verified against the live API: spot bookTicker/depth carry no timestamp, while
futures bookTicker has ``time`` and futures depth has ``E``/``T``.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.streaming import StreamKind


@dataclass(frozen=True, slots=True)
class VenueRoutes:
    """REST routes for one instrument class."""

    rest_base: str
    exchange_info: str
    book_ticker: str
    depth: str
    trades: str
    server_time: str
    # Rolling 24h statistics for every symbol, in one request.
    ticker_24hr: str
    # Futures only; spot has no funding.
    premium_index: str | None = None
    # Funding interval per symbol. Separate from premiumIndex, and it does not
    # list every symbol - the ones it omits have no publishable interval.
    funding_info: str | None = None

    def url(self, path: str) -> str:
        return f"{self.rest_base.rstrip('/')}{path}"


SPOT_ROUTES = VenueRoutes(
    rest_base="https://api.binance.com",
    exchange_info="/api/v3/exchangeInfo",
    book_ticker="/api/v3/ticker/bookTicker",
    depth="/api/v3/depth",
    trades="/api/v3/trades",
    server_time="/api/v3/time",
    ticker_24hr="/api/v3/ticker/24hr",
)

FUTURES_ROUTES = VenueRoutes(
    rest_base="https://fapi.binance.com",
    exchange_info="/fapi/v1/exchangeInfo",
    book_ticker="/fapi/v1/ticker/bookTicker",
    depth="/fapi/v1/depth",
    trades="/fapi/v1/trades",
    server_time="/fapi/v1/time",
    ticker_24hr="/fapi/v1/ticker/24hr",
    premium_index="/fapi/v1/premiumIndex",
    funding_info="/fapi/v1/fundingInfo",
)

# The venue rejects any other depth limit with -4021.
VALID_DEPTH_LIMITS = (5, 10, 20, 50, 100, 500, 1000)


def routes_for(market_type: MarketType) -> VenueRoutes:
    if market_type is MarketType.SPOT:
        return SPOT_ROUTES
    return FUTURES_ROUTES


def normalize_depth_limit(levels: int) -> int:
    """Snap to the nearest allowed depth limit at or above ``levels``."""
    for allowed in VALID_DEPTH_LIMITS:
        if levels <= allowed:
            return allowed
    return VALID_DEPTH_LIMITS[-1]


# --- WebSocket ---------------------------------------------------------------
#
# Combined streams (``/stream?streams=a/b/c``) wrap every message as
# ``{"stream": name, "data": payload}``, which lets one connection carry many
# markets.
#
# USD-M futures split their streams by route. Verified live on 2026-09-11:
# bookTicker and depth are served under /public, the 24h ticker under /market.
# The legacy /stream route still carries bookTicker and depth, but accepts a
# @ticker subscription and then sends nothing at all - a silent failure, which
# is why routing is explicit here and why idle connections are treated as dead.
SPOT_WS_BASE = "wss://stream.binance.com:9443"
FUTURES_WS_BASE = "wss://fstream.binance.com"

# Futures allow 200 streams per connection and spot 1024; the lower figure is
# used for both so one setting cannot exceed either venue's limit.
MAX_STREAMS_PER_CONNECTION = 200


def ws_path(market_type: MarketType, kind: StreamKind) -> str:
    """Combined-stream path for one kind of stream on one instrument class."""
    if market_type is MarketType.SPOT:
        return "/stream"
    return "/market/stream" if kind is StreamKind.TICKER else "/public/stream"
