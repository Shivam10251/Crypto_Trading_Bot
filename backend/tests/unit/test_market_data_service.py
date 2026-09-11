"""Service start-up: configured symbols must exist and trade, or nothing starts."""

from __future__ import annotations

import pytest

from trading_bot.core.config import MarketsConfig
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.errors import UnknownMarketError
from trading_bot.exchange.models import MarketRef, MarketSpec
from trading_bot.marketdata.service import resolve_markets


def spec(symbol: str, market_type: MarketType, *, active: bool = True) -> MarketSpec:
    return MarketSpec(
        ref=MarketRef("binance", symbol, market_type),
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
        is_active=active,
    )


class ListingVenue:
    """Just what start-up touches: a venue name and its listings."""

    venue = "binance"

    def __init__(self, *specs: MarketSpec) -> None:
        self._specs = specs
        self.requests: list[MarketType | None] = []

    async def get_markets(self, market_type: MarketType | None = None) -> list[MarketSpec]:
        self.requests.append(market_type)
        return [s for s in self._specs if s.ref.market_type is market_type]


LISTED = (
    spec("BTCUSDT", MarketType.SPOT),
    spec("ETHUSDT", MarketType.SPOT),
    spec("BTCUSDT", MarketType.PERPETUAL),
    spec("LUNAUSDT", MarketType.SPOT, active=False),
)


class TestResolveMarkets:
    async def test_markets_resolve_in_configured_order(self) -> None:
        venue = ListingVenue(*LISTED)
        config = MarketsConfig(spot_symbols=["ETHUSDT", "BTCUSDT"], perpetual_symbols=["BTCUSDT"])
        specs = await resolve_markets(venue, config)  # type: ignore[arg-type]
        assert [(s.symbol, s.ref.market_type) for s in specs] == [
            ("ETHUSDT", MarketType.SPOT),
            ("BTCUSDT", MarketType.SPOT),
            ("BTCUSDT", MarketType.PERPETUAL),
        ]
        # One listing request per instrument class, not one per symbol.
        assert venue.requests == [MarketType.SPOT, MarketType.PERPETUAL]

    async def test_an_unknown_symbol_stops_start_up(self) -> None:
        config = MarketsConfig(spot_symbols=["BTCUSDT"], perpetual_symbols=["DOGEUSDT"])
        with pytest.raises(UnknownMarketError, match="DOGEUSDT perpetual"):
            await resolve_markets(ListingVenue(*LISTED), config)  # type: ignore[arg-type]

    async def test_a_halted_symbol_stops_start_up(self) -> None:
        config = MarketsConfig(spot_symbols=["LUNAUSDT"], perpetual_symbols=[])
        with pytest.raises(UnknownMarketError, match=r"not trading.*LUNAUSDT spot"):
            await resolve_markets(ListingVenue(*LISTED), config)  # type: ignore[arg-type]
