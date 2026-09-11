"""Choosing markets: rank by the weaker leg, exclusions, explicit overrides."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from trading_bot.core.config import MarketsConfig, UniverseConfig
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.errors import UnknownMarketError
from trading_bot.exchange.models import MarketRef, MarketSpec, TickerStats
from trading_bot.monitoring.universe import Universe, select_universe

SPOT, PERP = MarketType.SPOT, MarketType.PERPETUAL
T0 = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
MILLION = Decimal(1_000_000)


def spec(symbol: str, kind: MarketType, *, active: bool = True, quote: str = "USDT") -> MarketSpec:
    return MarketSpec(
        ref=MarketRef("binance", symbol, kind),
        base_asset=symbol.removesuffix(quote),
        quote_asset=quote,
        is_active=active,
    )


class Venue:
    """Only what selection touches: listings and bulk 24h statistics."""

    venue = "binance"

    def __init__(self, specs: list[MarketSpec], volumes: dict[tuple[str, MarketType], int]):
        self._specs = specs
        self._volumes = volumes
        self.requests: list[tuple[str, MarketType | None]] = []

    async def get_markets(self, market_type: MarketType | None = None) -> list[MarketSpec]:
        self.requests.append(("markets", market_type))
        return [s for s in self._specs if s.ref.market_type is market_type]

    async def get_daily_stats(self, market_type: MarketType) -> list[TickerStats]:
        self.requests.append(("stats", market_type))
        return [
            TickerStats(
                ref=MarketRef("binance", symbol, kind),
                last_price=Decimal(1),
                volume=Decimal(1),
                quote_volume=Decimal(millions) * MILLION,
                exchange_timestamp=T0,
                local_timestamp=T0,
            )
            for (symbol, kind), millions in self._volumes.items()
            if kind is market_type
        ]


def listing() -> Venue:
    specs = [
        spec("BTCUSDT", SPOT),
        spec("BTCUSDT", PERP),
        spec("ETHUSDT", SPOT),
        spec("ETHUSDT", PERP),
        spec("SOLUSDT", SPOT),
        spec("SOLUSDT", PERP),
        spec("ZECUSDT", SPOT),
        spec("ZECUSDT", PERP),
        spec("USDCUSDT", SPOT),  # pegged: excluded by default
        spec("USDCUSDT", PERP),
        spec("PEPEUSDT", SPOT),  # the perpetual is 1000PEPEUSDT: not a pair
        spec("1000PEPEUSDT", PERP),
        spec("HALTUSDT", SPOT, active=False),
        spec("HALTUSDT", PERP),
        spec("ETHBTC", SPOT, quote="BTC"),  # another quote asset entirely
    ]
    volumes = {  # 24h quote volume, in millions
        ("BTCUSDT", SPOT): 1000,
        ("BTCUSDT", PERP): 9000,
        ("ETHUSDT", SPOT): 900,
        ("ETHUSDT", PERP): 8000,
        ("SOLUSDT", SPOT): 5000,  # the biggest spot market, with the thinnest perpetual
        ("SOLUSDT", PERP): 100,
        ("ZECUSDT", SPOT): 300,
        ("ZECUSDT", PERP): 2800,
        ("USDCUSDT", SPOT): 2000,
        ("USDCUSDT", PERP): 2000,
        ("PEPEUSDT", SPOT): 500,
        ("1000PEPEUSDT", PERP): 900,
        ("HALTUSDT", SPOT): 50,
        ("HALTUSDT", PERP): 50,
    }
    return Venue(specs, volumes)


def ranking(count: int = 3, **rule: Any) -> MarketsConfig:
    return MarketsConfig(
        selection="top_volume",
        top_volume=UniverseConfig(**{"count": count, "min_quote_volume": 0, **rule}),
        spot_symbols=[],
        perpetual_symbols=[],
    )


def symbols(universe: Universe) -> set[str]:
    return {ref.symbol for ref in universe.refs}


class TestRanking:
    async def test_pairs_rank_by_their_weaker_leg(self) -> None:
        """SOL has the biggest spot market but the thinnest perpetual, so it ranks last."""
        universe = await select_universe(listing(), ranking(count=3))  # type: ignore[arg-type]
        assert [(ref.symbol, ref.market_type) for ref in universe.refs] == [
            ("BTCUSDT", SPOT),
            ("BTCUSDT", PERP),
            ("ETHUSDT", SPOT),
            ("ETHUSDT", PERP),
            ("ZECUSDT", SPOT),
            ("ZECUSDT", PERP),
        ]
        ranks = {ref.symbol: rank for ref, rank in universe.ranks.items()}
        assert ranks == {"BTCUSDT": 1, "ETHUSDT": 2, "ZECUSDT": 3}

    async def test_the_count_is_configuration(self) -> None:
        universe = await select_universe(listing(), ranking(count=4))  # type: ignore[arg-type]
        assert len(universe.refs) == 8
        assert "SOLUSDT" in symbols(universe)

    async def test_exclusions_are_counted_by_reason(self) -> None:
        universe = await select_universe(listing(), ranking(count=10))  # type: ignore[arg-type]
        assert universe.excluded == {
            "no perpetual": 1,
            "not trading": 1,
            "excluded by configuration": 1,
        }
        assert universe.candidates == 6

    async def test_the_volume_floor_applies_to_the_weaker_leg(self) -> None:
        config = ranking(count=10, min_quote_volume=200_000_000)
        universe = await select_universe(listing(), config)  # type: ignore[arg-type]
        assert "SOLUSDT" not in symbols(universe)
        assert universe.excluded["below minimum volume"] == 1

    async def test_exclusion_lists_come_from_configuration(self) -> None:
        config = ranking(count=10, exclude_base_assets=[], exclude_symbols=["eThUsDt"])
        universe = await select_universe(listing(), config)  # type: ignore[arg-type]
        assert "USDCUSDT" in symbols(universe)
        assert "ETHUSDT" not in symbols(universe)

    async def test_the_universe_describes_itself(self) -> None:
        universe = await select_universe(listing(), ranking(count=3))  # type: ignore[arg-type]
        assert universe.describe() == ("top 3 of 6 spot/perpetual pairs by weaker-leg 24h volume")


class TestExplicitMarkets:
    async def test_configured_markets_join_the_ranking_without_duplicates(self) -> None:
        config = MarketsConfig(
            selection="top_volume",
            top_volume=UniverseConfig(count=1, min_quote_volume=0),
            spot_symbols=["BTCUSDT", "SOLUSDT"],
            perpetual_symbols=["BTCUSDT"],
        )
        universe = await select_universe(listing(), config)  # type: ignore[arg-type]
        assert [(ref.symbol, ref.market_type) for ref in universe.refs] == [
            ("BTCUSDT", SPOT),
            ("BTCUSDT", PERP),
            ("SOLUSDT", SPOT),
        ]
        assert MarketRef("binance", "SOLUSDT", SPOT) not in universe.ranks
        assert universe.describe().endswith("+ 1 configured")

    async def test_explicit_mode_needs_no_volume_data(self) -> None:
        venue = listing()
        config = MarketsConfig(spot_symbols=["ETHUSDT"], perpetual_symbols=[])
        universe = await select_universe(venue, config)  # type: ignore[arg-type]
        assert [ref.symbol for ref in universe.refs] == ["ETHUSDT"]
        assert venue.requests == [("markets", SPOT)]
        assert universe.describe() == "1 configured market"

    async def test_an_unknown_symbol_stops_start_up(self) -> None:
        config = MarketsConfig(spot_symbols=["BTCUSDT"], perpetual_symbols=["DOGEUSDT"])
        with pytest.raises(UnknownMarketError, match="DOGEUSDT perpetual"):
            await select_universe(listing(), config)  # type: ignore[arg-type]

    async def test_a_halted_symbol_stops_start_up(self) -> None:
        config = MarketsConfig(spot_symbols=["HALTUSDT"], perpetual_symbols=[])
        with pytest.raises(UnknownMarketError, match=r"not trading.*HALTUSDT spot"):
            await select_universe(listing(), config)  # type: ignore[arg-type]

    async def test_an_empty_selection_is_refused(self) -> None:
        config = MarketsConfig(spot_symbols=[], perpetual_symbols=[])
        with pytest.raises(UnknownMarketError, match="no markets selected"):
            await select_universe(listing(), config)  # type: ignore[arg-type]
