"""Bulk 24h statistics - the input to ranking markets by liquidity.

Fixtures are the first entries of the live ``ticker/24hr`` responses recorded on
2026-09-11.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.unit.test_exchange_interface import FakeExchangeAdapter
from trading_bot.core.config import ExchangeConfig
from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.binance import BinanceExchangeAdapter
from trading_bot.exchange.binance.rest import BinanceRestClient
from trading_bot.exchange.errors import ExchangeDataError, NotSupportedError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "binance"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def adapter_serving(
    payload: Any, requests: list[httpx.Request] | None = None
) -> BinanceExchangeAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, json=payload)

    client = BinanceRestClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return BinanceExchangeAdapter(ExchangeConfig(), client=client)


class TestDailyStats:
    async def test_entries_become_ticker_stats(self) -> None:
        payload = fixture("spot_ticker_24hr")
        stats = await adapter_serving(payload).get_daily_stats(MarketType.SPOT)
        assert [s.ref.symbol for s in stats] == [entry["symbol"] for entry in payload]
        first, entry = stats[0], payload[0]
        assert first.ref.market_type is MarketType.SPOT
        assert first.quote_volume == Decimal(entry["quoteVolume"])
        assert first.volume == Decimal(entry["volume"])
        assert first.last_price == Decimal(entry["lastPrice"])
        assert first.exchange_timestamp == datetime.fromtimestamp(entry["closeTime"] / 1000, tz=UTC)

    async def test_the_whole_venue_costs_one_request(self) -> None:
        requests: list[httpx.Request] = []
        adapter = adapter_serving(fixture("futures_ticker_24hr"), requests)
        stats = await adapter.get_daily_stats(MarketType.PERPETUAL)
        assert len(requests) == 1
        assert requests[0].url.host == "fapi.binance.com"
        assert requests[0].url.path == "/fapi/v1/ticker/24hr"
        assert not requests[0].url.params  # no symbol filter: every market at once
        assert all(s.ref.market_type is MarketType.PERPETUAL for s in stats)

    async def test_untradeable_entries_are_skipped_not_fatal(self) -> None:
        recorded = fixture("spot_ticker_24hr")
        halted = {**recorded[0], "symbol": "HALTEDUSDT", "lastPrice": "0.00000000"}
        payload = [*recorded, halted, {"no": "symbol"}]
        stats = await adapter_serving(payload).get_daily_stats(MarketType.SPOT)
        assert "HALTEDUSDT" not in {s.ref.symbol for s in stats}
        assert len(stats) == len(recorded)

    async def test_a_payload_that_is_not_a_list_is_rejected(self) -> None:
        adapter = adapter_serving({"code": -1, "msg": "unexpected"})
        with pytest.raises(ExchangeDataError, match="not a list"):
            await adapter.get_daily_stats(MarketType.SPOT)

    async def test_venues_without_bulk_statistics_say_so(self) -> None:
        with pytest.raises(NotSupportedError, match="bulk 24h"):
            await FakeExchangeAdapter().get_daily_stats(MarketType.SPOT)
