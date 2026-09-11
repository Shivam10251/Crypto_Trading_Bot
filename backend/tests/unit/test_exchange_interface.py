"""The boundary itself.

The point of ``ExchangeAdapter`` is that a second venue can be added without
touching strategy code. These tests prove the interface is implementable by
something that is not Binance, and that the capability guards behave.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.base import ExchangeAdapter
from trading_bot.exchange.errors import ExecutionNotEnabledError, NotSupportedError
from trading_bot.exchange.models import (
    BookLevel,
    MarketDataSubscription,
    MarketRef,
    MarketSpec,
    OrderBook,
    Quote,
    ServerTime,
    TradePrint,
)


class FakeExchangeAdapter(ExchangeAdapter):
    """A second 'venue' built only from the interface - no Binance anywhere.

    If this class needs a Binance import or an awkward workaround to exist, the
    abstraction has leaked.
    """

    venue = "fake_exchange"

    def __init__(self, bid: str = "500", ask: str = "501") -> None:
        self._bid = Decimal(bid)
        self._ask = Decimal(ask)

    async def get_markets(self, market_type: MarketType | None = None) -> list[MarketSpec]:
        kind = market_type or MarketType.SPOT
        return [
            MarketSpec(
                ref=self.market_ref("XYZUSD", kind),
                base_asset="XYZ",
                quote_asset="USD",
                is_active=True,
                tick_size=Decimal("0.5"),
            )
        ]

    async def get_ticker(self, ref: MarketRef) -> Quote:
        return Quote(
            ref=ref,
            bid=self._bid,
            ask=self._ask,
            bid_size=Decimal("10"),
            ask_size=Decimal("10"),
            local_timestamp=datetime.now(UTC),
        )

    async def get_order_book(self, ref: MarketRef, levels: int = 10) -> OrderBook:
        return OrderBook(
            ref=ref,
            bids=(BookLevel(self._bid, Decimal("10")),),
            asks=(BookLevel(self._ask, Decimal("10")),),
            local_timestamp=datetime.now(UTC),
        )

    async def get_recent_trades(self, ref: MarketRef, limit: int = 50) -> list[TradePrint]:
        return []

    async def get_server_time(self) -> ServerTime:
        now = datetime.now(UTC)
        return ServerTime(exchange_time=now, local_time=now, round_trip_ms=1)

    async def subscribe_market_data(
        self, subscription: MarketDataSubscription
    ) -> AsyncIterator[Quote]:
        for ref in subscription.refs:
            yield await self.get_ticker(ref)


class TestReplaceability:
    async def test_a_non_binance_adapter_satisfies_the_interface(self) -> None:
        adapter = FakeExchangeAdapter()
        assert isinstance(adapter, ExchangeAdapter)
        quote = await adapter.get_ticker(adapter.market_ref("XYZUSD", MarketType.SPOT))
        assert quote.ref.venue == "fake_exchange"
        assert quote.mid_price == Decimal("500.5")

    async def test_consumers_are_venue_agnostic(self) -> None:
        """A function written against the interface works for any adapter."""

        async def best_mid(adapter: ExchangeAdapter, symbol: str) -> Decimal:
            quote = await adapter.get_ticker(adapter.market_ref(symbol, MarketType.SPOT))
            return quote.mid_price

        assert await best_mid(FakeExchangeAdapter("100", "102"), "XYZUSD") == Decimal("101")

    async def test_streaming_can_be_implemented_by_an_adapter(self) -> None:
        adapter = FakeExchangeAdapter()
        ref = adapter.market_ref("XYZUSD", MarketType.SPOT)
        received = [
            quote
            async for quote in adapter.subscribe_market_data(
                MarketDataSubscription.top_of_book(ref)
            )
        ]
        assert len(received) == 1


class TestCapabilityGuards:
    async def test_unsupported_funding_raises_rather_than_returning_zero(self) -> None:
        adapter = FakeExchangeAdapter()
        with pytest.raises(NotSupportedError, match="funding"):
            await adapter.get_funding(adapter.market_ref("XYZUSD", MarketType.SPOT))

    async def test_execution_is_disabled_for_every_adapter(self) -> None:
        """The guard lives on the base class, so no adapter can trade by accident."""
        adapter = FakeExchangeAdapter()
        with pytest.raises(ExecutionNotEnabledError):
            await adapter.place_order()


class TestAbstractEnforcement:
    def test_incomplete_adapter_cannot_be_instantiated(self) -> None:
        class Incomplete(ExchangeAdapter):
            venue = "incomplete"

        with pytest.raises(TypeError, match="abstract"):
            Incomplete()  # type: ignore[abstract]

    def test_required_market_data_methods(self) -> None:
        assert ExchangeAdapter.__abstractmethods__ == frozenset(
            {
                "get_markets",
                "get_ticker",
                "get_order_book",
                "get_recent_trades",
                "get_server_time",
                "subscribe_market_data",
            }
        )
