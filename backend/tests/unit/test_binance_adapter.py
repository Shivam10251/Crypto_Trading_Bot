"""Adapter behaviour with a mocked transport.

Focus: routing spot vs futures to the right host, honouring the venue's depth
limits, mapping "unknown symbol" to our own error, and keeping execution
firmly disabled.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from trading_bot.core.config import ExchangeConfig
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.binance import VENUE, BinanceExchangeAdapter
from trading_bot.exchange.binance.endpoints import normalize_depth_limit
from trading_bot.exchange.binance.rest import BinanceRestClient
from trading_bot.exchange.errors import (
    ExecutionNotEnabledError,
    NotSupportedError,
    UnknownMarketError,
)
from trading_bot.exchange.models import MarketDataSubscription

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "binance"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


# Routes requests to the matching recorded payload, and records what was asked.
def make_adapter(recorder: list[httpx.Request] | None = None) -> BinanceExchangeAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        host, path = request.url.host, request.url.path
        spot = host == "api.binance.com"
        if path.endswith("exchangeInfo"):
            return httpx.Response(
                200, json=fixture("spot_exchange_info" if spot else "futures_exchange_info")
            )
        if path.endswith("bookTicker"):
            if request.url.params.get("symbol") == "NOSUCHUSDT":
                return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
            return httpx.Response(
                200, json=fixture("spot_book_ticker" if spot else "futures_book_ticker")
            )
        if path.endswith("depth"):
            return httpx.Response(200, json=fixture("spot_depth" if spot else "futures_depth"))
        if path.endswith("trades"):
            return httpx.Response(200, json=fixture("spot_trades"))
        if path.endswith("avgPrice"):
            return httpx.Response(200, json={"mins": 5, "price": "76980.25"})
        if path.endswith("premiumIndex"):
            return httpx.Response(200, json=fixture("futures_premium_index"))
        if path.endswith("time"):
            return httpx.Response(200, json={"serverTime": 1789099685733})
        return httpx.Response(404, json={"msg": f"unmapped {path}"})

    client = BinanceRestClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return BinanceExchangeAdapter(ExchangeConfig(), client=client)


class TestRouting:
    """Spot and futures are different hosts; the adapter must pick correctly."""

    async def test_spot_ticker_goes_to_spot_host(self) -> None:
        seen: list[httpx.Request] = []
        adapter = make_adapter(seen)
        ref = adapter.market_ref("BTCUSDT", MarketType.SPOT)
        await adapter.get_ticker(ref)
        assert seen[-1].url.host == "api.binance.com"
        assert seen[-1].url.path == "/api/v3/ticker/bookTicker"

    async def test_perpetual_ticker_goes_to_futures_host(self) -> None:
        seen: list[httpx.Request] = []
        adapter = make_adapter(seen)
        ref = adapter.market_ref("BTCUSDT", MarketType.PERPETUAL)
        await adapter.get_ticker(ref)
        assert seen[-1].url.host == "fapi.binance.com"
        assert seen[-1].url.path == "/fapi/v1/ticker/bookTicker"

    async def test_symbols_are_upper_cased(self) -> None:
        seen: list[httpx.Request] = []
        adapter = make_adapter(seen)
        await adapter.get_ticker(adapter.market_ref("btcusdt", MarketType.SPOT))
        assert seen[-1].url.params["symbol"] == "BTCUSDT"

    async def test_refs_carry_the_venue(self) -> None:
        adapter = make_adapter()
        assert adapter.market_ref("BTCUSDT", MarketType.SPOT).venue == VENUE


class TestMarketData:
    async def test_spot_and_perp_quotes_differ_in_clock_availability(self) -> None:
        """The asymmetry is real and must survive the adapter layer."""
        adapter = make_adapter()
        spot = await adapter.get_ticker(adapter.market_ref("BTCUSDT", MarketType.SPOT))
        perp = await adapter.get_ticker(adapter.market_ref("BTCUSDT", MarketType.PERPETUAL))
        assert spot.exchange_timestamp is None
        assert perp.exchange_timestamp is not None

    async def test_get_markets_loads_both_types_by_default(self) -> None:
        adapter = make_adapter()
        specs = await adapter.get_markets()
        types = {spec.ref.market_type for spec in specs}
        assert types == {MarketType.SPOT, MarketType.PERPETUAL}

    async def test_get_markets_can_filter_to_one_type(self) -> None:
        seen: list[httpx.Request] = []
        adapter = make_adapter(seen)
        specs = await adapter.get_markets(MarketType.SPOT)
        assert {spec.ref.market_type for spec in specs} == {MarketType.SPOT}
        assert len(seen) == 1

    async def test_quarterly_futures_are_excluded_from_perpetuals(self) -> None:
        """Futures exchangeInfo mixes delivery contracts with perpetuals."""
        adapter = make_adapter()
        specs = await adapter.get_markets(MarketType.PERPETUAL)
        assert [spec.symbol for spec in specs] == ["BTCUSDT", "IOSTUSDT"]

    async def test_order_book_is_trimmed_to_requested_levels(self) -> None:
        """The venue rounds the limit up; the caller still gets what it asked."""
        adapter = make_adapter()
        book = await adapter.get_order_book(
            adapter.market_ref("BTCUSDT", MarketType.PERPETUAL), levels=2
        )
        assert len(book.bids) == 2
        assert len(book.asks) == 2
        assert not book.bids_complete and not book.asks_complete

    async def test_depth_limit_is_snapped_to_an_allowed_value(self) -> None:
        seen: list[httpx.Request] = []
        adapter = make_adapter(seen)
        await adapter.get_order_book(adapter.market_ref("BTCUSDT", MarketType.SPOT), levels=3)
        # 3 is rejected by the venue with -4021; 5 is the nearest allowed.
        assert seen[-1].url.params["limit"] == "5"

    @pytest.mark.parametrize(
        ("requested", "expected"), [(1, 5), (5, 5), (7, 10), (11, 20), (5000, 1000)]
    )
    def test_depth_limit_normalisation(self, requested: int, expected: int) -> None:
        assert normalize_depth_limit(requested) == expected

    async def test_recent_trades_are_parsed(self) -> None:
        adapter = make_adapter()
        trades = await adapter.get_recent_trades(
            adapter.market_ref("BTCUSDT", MarketType.SPOT), limit=2
        )
        assert len(trades) == 2
        assert all(trade.price > 0 for trade in trades)

    async def test_server_time_measures_skew(self) -> None:
        adapter = make_adapter()
        clock = await adapter.get_server_time()
        assert clock.round_trip_ms >= 0
        assert isinstance(clock.skew_ms, int)

    async def test_spot_average_price_is_parsed_for_venue_filters(self) -> None:
        adapter = make_adapter()
        price = await adapter.get_average_price(adapter.market_ref("BTCUSDT", MarketType.SPOT))
        assert price == Decimal("76980.25")

    async def test_futures_average_price_is_not_faked(self) -> None:
        adapter = make_adapter()
        with pytest.raises(NotSupportedError, match="no average-price endpoint"):
            await adapter.get_average_price(adapter.market_ref("BTCUSDT", MarketType.PERPETUAL))

    @pytest.mark.parametrize("levels", [0, -5])
    async def test_invalid_depth_request_rejected(self, levels: int) -> None:
        adapter = make_adapter()
        with pytest.raises(ValueError, match="must be positive"):
            await adapter.get_order_book(
                adapter.market_ref("BTCUSDT", MarketType.SPOT), levels=levels
            )


class TestFunding:
    async def test_perpetual_funding_is_available(self) -> None:
        adapter = make_adapter()
        funding = await adapter.get_funding(adapter.market_ref("BTCUSDT", MarketType.PERPETUAL))
        assert funding.last_funding_rate != 0
        assert funding.mark_price > 0

    async def test_spot_funding_is_refused_not_faked(self) -> None:
        adapter = make_adapter()
        with pytest.raises(NotSupportedError, match="no funding rate"):
            await adapter.get_funding(adapter.market_ref("BTCUSDT", MarketType.SPOT))


class TestErrorTranslation:
    async def test_unknown_symbol_becomes_unknown_market(self) -> None:
        adapter = make_adapter()
        with pytest.raises(UnknownMarketError, match="NOSUCHUSDT"):
            await adapter.get_ticker(adapter.market_ref("NOSUCHUSDT", MarketType.SPOT))


class TestExecutionIsDisabled:
    """Phase 2 is market data only. Execution must fail loudly, not silently."""

    async def test_place_order_raises(self) -> None:
        adapter = make_adapter()
        with pytest.raises(ExecutionNotEnabledError, match="Phase 17"):
            await adapter.place_order()

    async def test_cancel_order_raises(self) -> None:
        adapter = make_adapter()
        with pytest.raises(ExecutionNotEnabledError):
            await adapter.cancel_order()

    async def test_order_status_raises(self) -> None:
        adapter = make_adapter()
        with pytest.raises(ExecutionNotEnabledError):
            await adapter.get_order_status()

    async def test_balances_raise(self) -> None:
        adapter = make_adapter()
        with pytest.raises(ExecutionNotEnabledError, match="authenticated"):
            await adapter.get_balances()

    def test_adapter_supplies_stream_routing(self) -> None:
        """The adapter's half of streaming; the engine owns the connections."""
        adapter = make_adapter()
        ref = adapter.market_ref("BTCUSDT", MarketType.SPOT)
        [endpoint] = adapter.stream_source().endpoints(MarketDataSubscription.top_of_book(ref))
        assert endpoint.url == "wss://stream.binance.com:9443/stream?streams=btcusdt@bookTicker"


class TestLifecycle:
    async def test_works_as_an_async_context_manager(self) -> None:
        async with make_adapter() as adapter:
            quote = await adapter.get_ticker(adapter.market_ref("BTCUSDT", MarketType.SPOT))
        assert quote.bid > 0

    async def test_injected_client_is_not_closed_by_the_adapter(self) -> None:
        """Whoever owns the client closes it; the adapter must not surprise them."""
        client = BinanceRestClient(
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
            )
        )
        adapter = BinanceExchangeAdapter(ExchangeConfig(), client=client)
        await adapter.aclose()
        # Still usable: the adapter did not own it.
        assert await client.get("https://api.binance.com/api/v3/time") == {}


class TestTickRounding:
    @pytest.mark.parametrize(
        ("price", "tick", "expected"),
        [
            ("76986.117", "0.01", "76986.12"),
            ("76986.111", "0.01", "76986.11"),
            ("1.5", None, "1.5"),
        ],
    )
    def test_prices_snap_to_the_venue_tick(
        self, price: str, tick: str | None, expected: str
    ) -> None:
        from decimal import Decimal

        result = BinanceExchangeAdapter.round_to_tick(
            Decimal(price), Decimal(tick) if tick else None
        )
        assert result == Decimal(expected)
