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


@dataclass(frozen=True, slots=True)
class VenueRoutes:
    """REST and WebSocket routes for one instrument class."""

    rest_base: str
    ws_base: str
    exchange_info: str
    book_ticker: str
    depth: str
    trades: str
    server_time: str
    # Futures only; spot has no funding.
    premium_index: str | None = None

    def url(self, path: str) -> str:
        return f"{self.rest_base.rstrip('/')}{path}"


SPOT_ROUTES = VenueRoutes(
    rest_base="https://api.binance.com",
    ws_base="wss://stream.binance.com:9443/ws",
    exchange_info="/api/v3/exchangeInfo",
    book_ticker="/api/v3/ticker/bookTicker",
    depth="/api/v3/depth",
    trades="/api/v3/trades",
    server_time="/api/v3/time",
)

FUTURES_ROUTES = VenueRoutes(
    rest_base="https://fapi.binance.com",
    ws_base="wss://fstream.binance.com/ws",
    exchange_info="/fapi/v1/exchangeInfo",
    book_ticker="/fapi/v1/ticker/bookTicker",
    depth="/fapi/v1/depth",
    trades="/fapi/v1/trades",
    server_time="/fapi/v1/time",
    premium_index="/fapi/v1/premiumIndex",
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
