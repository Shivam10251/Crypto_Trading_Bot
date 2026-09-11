"""Funding polling: bulk, cached intervals, and surviving a failed request."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.base import ExchangeAdapter
from trading_bot.exchange.errors import ExchangeError, NotSupportedError
from trading_bot.exchange.models import FundingInfo, MarketRef
from trading_bot.marketdata.funding import FundingTracker

NOW = datetime(2026, 9, 11, 16, 30, tzinfo=UTC)
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)
ETH = MarketRef("binance", "ETHUSDT", MarketType.PERPETUAL)
DOGE = MarketRef("binance", "DOGEUSDT", MarketType.PERPETUAL)


def rate(ref: MarketRef, interval: int | None) -> FundingInfo:
    return FundingInfo(
        ref=ref,
        mark_price=Decimal(100),
        index_price=Decimal(100),
        last_funding_rate=Decimal("0.0001"),
        next_funding_time=NOW + timedelta(hours=4),
        local_timestamp=NOW,
        funding_interval_hours=interval,
    )


class FakeVenue(ExchangeAdapter):
    venue = "fake"

    def __init__(self, rates: list[FundingInfo], *, fail: bool = False) -> None:
        self._rates = rates
        self._fail = fail
        self.calls = 0

    async def get_funding_rates(self, market_type: MarketType) -> list[FundingInfo]:
        self.calls += 1
        if self._fail:
            raise ExchangeError("venue unreachable")
        return list(self._rates)

    # Unused market-data surface.
    async def get_markets(self, market_type: MarketType | None = None) -> list[object]:  # type: ignore[override]
        raise NotSupportedError("not needed")

    async def get_ticker(self, ref: MarketRef) -> object:  # type: ignore[override]
        raise NotSupportedError("not needed")

    async def get_order_book(self, ref: MarketRef, levels: int = 10) -> object:  # type: ignore[override]
        raise NotSupportedError("not needed")

    async def get_recent_trades(self, ref: MarketRef, limit: int = 100) -> list[object]:  # type: ignore[override]
        raise NotSupportedError("not needed")

    async def get_server_time(self) -> object:  # type: ignore[override]
        raise NotSupportedError("not needed")

    def stream_source(self) -> object:  # type: ignore[override]
        raise NotSupportedError("not needed")


async def test_only_monitored_perpetuals_are_kept() -> None:
    """One bulk request covers the venue; we keep the markets we follow."""
    venue = FakeVenue([rate(PERP, 8), rate(ETH, 4), rate(DOGE, 8)])
    tracker = FundingTracker(venue, [SPOT, PERP, ETH])
    assert await tracker.refresh() == 2
    assert set(tracker.rates) == {PERP, ETH}


async def test_spot_markets_are_never_polled_for_funding() -> None:
    venue = FakeVenue([])
    tracker = FundingTracker(venue, [SPOT])
    assert await tracker.refresh() == 0
    assert venue.calls == 0  # no perpetuals to ask about


async def test_markets_without_a_published_interval_are_named() -> None:
    """Their funding cannot be priced, so the count must be visible."""
    venue = FakeVenue([rate(PERP, 8), rate(ETH, None)])
    tracker = FundingTracker(venue, [PERP, ETH])
    await tracker.refresh()
    assert tracker.markets_without_interval == {"ETHUSDT"}


async def test_a_failed_poll_keeps_the_last_known_rates() -> None:
    """Funding is slow-moving: stale beats unpriceable."""
    venue = FakeVenue([rate(PERP, 8)])
    tracker = FundingTracker(venue, [PERP], interval_seconds=0.01)
    await tracker.refresh()
    venue._fail = True
    with pytest.raises(ExchangeError):
        await tracker.refresh()
    assert set(tracker.rates) == {PERP}


async def test_a_non_positive_interval_is_rejected() -> None:
    with pytest.raises(ValueError, match="interval_seconds"):
        FundingTracker(FakeVenue([]), [PERP], interval_seconds=0)
