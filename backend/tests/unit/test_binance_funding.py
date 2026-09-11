"""Bulk funding rates and settlement intervals.

Fixtures are live ``premiumIndex`` and ``fundingInfo`` responses recorded on
2026-09-11, chosen to cover every case the venue actually presents: 8h, 4h and
1h settlement, plus a symbol ``fundingInfo`` omits entirely.
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
from trading_bot.exchange.binance import BinanceExchangeAdapter
from trading_bot.exchange.binance.mapping import parse_funding_intervals
from trading_bot.exchange.binance.rest import BinanceRestClient
from trading_bot.exchange.errors import ExchangeDataError, NotSupportedError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "binance"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def adapter_serving(
    routes: dict[str, Any], requests: list[httpx.Request] | None = None
) -> BinanceExchangeAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        for path, payload in routes.items():
            if request.url.path == path:
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"code": -1121, "msg": "unknown route"})

    client = BinanceRestClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return BinanceExchangeAdapter(ExchangeConfig(), client=client)


def live_adapter(requests: list[httpx.Request] | None = None) -> BinanceExchangeAdapter:
    return adapter_serving(
        {
            "/fapi/v1/premiumIndex": fixture("futures_premium_index_bulk"),
            "/fapi/v1/fundingInfo": fixture("futures_funding_info"),
        },
        requests,
    )


class TestFundingIntervals:
    def test_intervals_are_parsed_per_symbol(self) -> None:
        intervals = parse_funding_intervals(fixture("futures_funding_info"))
        assert intervals["BTCUSDT"] == 8
        assert intervals["ENAUSDT"] == 4
        assert intervals["IOSTUSDT"] == 1

    def test_the_venue_really_does_mix_intervals(self) -> None:
        """The reason funding cannot be scaled from a hard-coded eight hours."""
        intervals = parse_funding_intervals(fixture("futures_funding_info"))
        assert len(set(intervals.values())) > 1

    def test_a_symbol_the_venue_omits_is_absent_not_defaulted(self) -> None:
        intervals = parse_funding_intervals(fixture("futures_funding_info"))
        assert "MYROUSDT" not in intervals

    def test_a_non_list_payload_is_refused(self) -> None:
        with pytest.raises(ExchangeDataError, match="fundingInfo"):
            parse_funding_intervals({"symbol": "BTCUSDT"})

    def test_malformed_entries_are_skipped(self) -> None:
        intervals = parse_funding_intervals(
            [
                {"symbol": "BTCUSDT", "fundingIntervalHours": 8},
                {"symbol": "BADUSDT", "fundingIntervalHours": 0},
                {"symbol": "WORSEUSDT"},
                "not a mapping",
            ]
        )
        assert intervals == {"BTCUSDT": 8}

    async def test_intervals_are_fetched_once_and_reused(self) -> None:
        """They change on the order of weeks; one request per process is enough."""
        requests: list[httpx.Request] = []
        adapter = live_adapter(requests)
        await adapter.get_funding_intervals(MarketType.PERPETUAL)
        await adapter.get_funding_intervals(MarketType.PERPETUAL)
        assert sum(r.url.path == "/fapi/v1/fundingInfo" for r in requests) == 1

    async def test_spot_has_no_funding_interval(self) -> None:
        with pytest.raises(NotSupportedError):
            await live_adapter().get_funding_intervals(MarketType.SPOT)


class TestBulkFundingRates:
    async def test_every_symbol_comes_back_with_its_interval(self) -> None:
        rates = await live_adapter().get_funding_rates(MarketType.PERPETUAL)
        by_symbol = {rate.ref.symbol: rate for rate in rates}
        assert by_symbol["BTCUSDT"].funding_interval_hours == 8
        assert by_symbol["ENAUSDT"].funding_interval_hours == 4
        assert by_symbol["IOSTUSDT"].funding_interval_hours == 1

    async def test_a_symbol_without_a_published_interval_reports_none(self) -> None:
        """Not eight hours by default - the caller must know it is unknown."""
        rates = await live_adapter().get_funding_rates(MarketType.PERPETUAL)
        omitted = next(r for r in rates if r.ref.symbol == "MYROUSDT")
        assert omitted.funding_interval_hours is None
        assert omitted.last_funding_rate is not None

    async def test_the_whole_venue_costs_one_request(self) -> None:
        """Fifty monitored pairs must not mean fifty requests."""
        requests: list[httpx.Request] = []
        rates = await live_adapter(requests).get_funding_rates(MarketType.PERPETUAL)
        assert len(rates) == len(fixture("futures_premium_index_bulk"))
        assert sum(r.url.path == "/fapi/v1/premiumIndex" for r in requests) == 1

    async def test_rates_carry_mark_and_index_prices(self) -> None:
        rates = await live_adapter().get_funding_rates(MarketType.PERPETUAL)
        btc = next(r for r in rates if r.ref.symbol == "BTCUSDT")
        assert btc.mark_price > 0
        assert btc.index_price > 0
        assert btc.ref.market_type is MarketType.PERPETUAL

    async def test_rate_bps_per_hours_scales_by_the_real_interval(self) -> None:
        rates = await live_adapter().get_funding_rates(MarketType.PERPETUAL)
        ena = next(r for r in rates if r.ref.symbol == "ENAUSDT")  # 4h
        # Eight hours is two intervals for this market, not one.
        assert ena.rate_bps_per(Decimal(8)) == ena.last_funding_rate_bps * 2

    async def test_rate_bps_per_hours_is_none_without_an_interval(self) -> None:
        rates = await live_adapter().get_funding_rates(MarketType.PERPETUAL)
        omitted = next(r for r in rates if r.ref.symbol == "MYROUSDT")
        assert omitted.rate_bps_per(Decimal(8)) is None

    async def test_malformed_entries_are_skipped_not_fatal(self) -> None:
        adapter = adapter_serving(
            {
                "/fapi/v1/premiumIndex": [
                    *fixture("futures_premium_index_bulk"),
                    {"symbol": "BROKENUSDT"},  # no prices
                    {"no": "symbol"},
                ],
                "/fapi/v1/fundingInfo": fixture("futures_funding_info"),
            }
        )
        rates = await adapter.get_funding_rates(MarketType.PERPETUAL)
        assert "BROKENUSDT" not in {r.ref.symbol for r in rates}
        assert len(rates) == len(fixture("futures_premium_index_bulk"))

    async def test_a_non_list_payload_is_refused(self) -> None:
        adapter = adapter_serving(
            {
                "/fapi/v1/premiumIndex": {"symbol": "BTCUSDT"},
                "/fapi/v1/fundingInfo": fixture("futures_funding_info"),
            }
        )
        with pytest.raises(ExchangeDataError, match="premiumIndex"):
            await adapter.get_funding_rates(MarketType.PERPETUAL)

    async def test_spot_markets_have_no_funding(self) -> None:
        with pytest.raises(NotSupportedError):
            await live_adapter().get_funding_rates(MarketType.SPOT)

    async def test_an_unreachable_interval_endpoint_does_not_fail_the_rates(self) -> None:
        """One endpoint's outage must not take down a call that otherwise works.

        The markets come back with unknown intervals, which the cost model
        refuses to price - visibly - rather than being defaulted to eight hours.
        """
        adapter = adapter_serving(
            {"/fapi/v1/premiumIndex": fixture("futures_premium_index_bulk")}
        )  # fundingInfo 404s
        rates = await adapter.get_funding_rates(MarketType.PERPETUAL)
        assert len(rates) == len(fixture("futures_premium_index_bulk"))
        assert all(rate.funding_interval_hours is None for rate in rates)

    async def test_single_symbol_funding_also_carries_its_interval(self) -> None:
        adapter = adapter_serving(
            {
                "/fapi/v1/premiumIndex": fixture("futures_premium_index"),
                "/fapi/v1/fundingInfo": fixture("futures_funding_info"),
            }
        )
        funding = await adapter.get_funding(adapter.market_ref("BTCUSDT", MarketType.PERPETUAL))
        assert funding.funding_interval_hours == 8
