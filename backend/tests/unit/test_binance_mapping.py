"""Parsing real Binance payloads into normalized models.

The fixtures in tests/fixtures/binance are verbatim responses captured from the
live API, so these tests fail if Binance changes a shape we depend on.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.binance.mapping import (
    parse_funding,
    parse_market_spec,
    parse_order_book,
    parse_quote,
    parse_trade,
    to_datetime,
    to_decimal,
)
from trading_bot.exchange.errors import ExchangeDataError
from trading_bot.exchange.models import MarketRef

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "binance"
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)


def load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class TestPrimitives:
    def test_decimal_keeps_venue_precision(self) -> None:
        """Binance sends numbers as strings so clients keep full precision."""
        assert to_decimal("76986.10000000", "x") == Decimal("76986.10000000")

    @pytest.mark.parametrize("raw", ["", "abc", None, "1.2.3"])
    def test_unparseable_number_raises(self, raw: object) -> None:
        with pytest.raises(ExchangeDataError, match="cannot parse"):
            to_decimal(raw, "ctx")

    def test_milliseconds_become_aware_utc(self) -> None:
        moment = to_datetime(1789099688293, "x")
        assert moment.tzinfo is UTC
        assert moment.year == 2026

    @pytest.mark.parametrize("raw", ["not-a-time", None, {}])
    def test_bad_timestamp_raises(self, raw: object) -> None:
        with pytest.raises(ExchangeDataError, match="timestamp"):
            to_datetime(raw, "ctx")


class TestQuoteParsing:
    def test_spot_quote_has_no_exchange_clock(self) -> None:
        """Verified against the live API: spot bookTicker omits any timestamp."""
        q = parse_quote(load("spot_book_ticker"), SPOT, NOW)
        assert q.bid == Decimal("76986.10000000")
        assert q.ask == Decimal("76986.11000000")
        assert q.exchange_timestamp is None
        assert q.latency_ms is None

    def test_futures_quote_carries_clock_and_sequence(self) -> None:
        q = parse_quote(load("futures_book_ticker"), PERP, NOW)
        assert q.bid == Decimal("76951.30")
        assert q.exchange_timestamp is not None
        assert q.sequence == 11528719920889

    def test_missing_field_raises(self) -> None:
        payload = load("spot_book_ticker")
        del payload["askPrice"]
        with pytest.raises(ExchangeDataError, match="askPrice"):
            parse_quote(payload, SPOT, NOW)

    def test_crossed_payload_is_refused(self) -> None:
        """Bad data must not reach a strategy, even if the venue sent it."""
        payload = load("spot_book_ticker")
        payload["bidPrice"], payload["askPrice"] = "76990.0", "76980.0"
        with pytest.raises(ExchangeDataError, match="crossed book"):
            parse_quote(payload, SPOT, NOW)

    def test_zero_price_is_refused(self) -> None:
        payload = load("spot_book_ticker")
        payload["bidPrice"] = "0"
        with pytest.raises(ExchangeDataError, match="non-positive"):
            parse_quote(payload, SPOT, NOW)


class TestOrderBookParsing:
    def test_spot_depth(self) -> None:
        b = parse_order_book(load("spot_depth"), SPOT, NOW)
        assert b.best_bid == Decimal("76986.10000000")
        assert b.best_ask == Decimal("76986.11000000")
        assert b.sequence == 99977308772
        assert b.exchange_timestamp is None

    def test_futures_depth_has_event_time(self) -> None:
        b = parse_order_book(load("futures_depth"), PERP, NOW)
        assert b.exchange_timestamp is not None
        assert len(b.bids) == 5

    def test_zero_size_levels_are_dropped(self) -> None:
        """Binance pads books with zero-size levels; they are not liquidity."""
        payload = load("spot_depth")
        payload["bids"].append(["76000.00", "0"])
        b = parse_order_book(payload, SPOT, NOW)
        assert all(level.size > 0 for level in b.bids)

    def test_malformed_level_raises(self) -> None:
        payload = load("spot_depth")
        payload["bids"] = [["76986.10"]]
        with pytest.raises(ExchangeDataError, match="malformed level"):
            parse_order_book(payload, SPOT, NOW)

    def test_book_with_no_real_liquidity_raises(self) -> None:
        payload = load("spot_depth")
        payload["bids"] = [["76986.10", "0"]]
        with pytest.raises(ExchangeDataError, match="no levels with size"):
            parse_order_book(payload, SPOT, NOW)


class TestTradeParsing:
    def test_aggressor_is_inverted_from_is_buyer_maker(self) -> None:
        """If the buyer was the maker, the seller crossed the spread."""
        trades = load("spot_trades")
        assert parse_trade(trades[0], SPOT, NOW).aggressor_side is Side.BUY
        assert parse_trade(trades[1], SPOT, NOW).aggressor_side is Side.SELL

    def test_trade_fields(self) -> None:
        trade = parse_trade(load("spot_trades")[0], SPOT, NOW)
        assert trade.trade_id == "6673529205"
        assert trade.price == Decimal("76986.11000000")
        assert trade.quantity == Decimal("0.00134000")
        assert trade.exchange_timestamp < trade.local_timestamp

    def test_zero_quantity_refused(self) -> None:
        payload = load("spot_trades")[0]
        payload["qty"] = "0"
        with pytest.raises(ExchangeDataError, match="invalid trade print"):
            parse_trade(payload, SPOT, NOW)


class TestMarketSpecParsing:
    def test_spot_filters_are_extracted(self) -> None:
        spec = parse_market_spec(
            load("spot_exchange_info")["symbols"][0], "binance", MarketType.SPOT
        )
        assert spec.symbol == "BTCUSDT"
        assert spec.base_asset == "BTC"
        assert spec.tick_size == Decimal("0.01000000")
        assert spec.step_size == Decimal("0.00001000")
        assert spec.min_notional == Decimal("5.00000000")
        assert spec.is_active is True

    def test_fees_are_left_unknown(self) -> None:
        """Binance exposes account fees only behind auth; never guess them."""
        spec = parse_market_spec(
            load("spot_exchange_info")["symbols"][0], "binance", MarketType.SPOT
        )
        assert spec.maker_fee_bps is None
        assert spec.taker_fee_bps is None

    def test_non_trading_symbol_is_inactive(self) -> None:
        spec = parse_market_spec(
            load("spot_exchange_info")["symbols"][2], "binance", MarketType.SPOT
        )
        assert spec.is_active is False

    def test_futures_margin_asset_becomes_settlement_asset(self) -> None:
        spec = parse_market_spec(
            load("futures_exchange_info")["symbols"][0], "binance", MarketType.PERPETUAL
        )
        assert spec.settlement_asset == "USDT"
        assert spec.tick_size == Decimal("0.10")

    def test_missing_base_asset_raises(self) -> None:
        with pytest.raises(ExchangeDataError, match="baseAsset"):
            parse_market_spec({"symbol": "X", "quoteAsset": "USDT"}, "binance", MarketType.SPOT)


class TestFundingParsing:
    def test_funding_rate_and_mark_price(self) -> None:
        funding = parse_funding(load("futures_premium_index"), PERP, NOW)
        assert funding.mark_price == Decimal("76951.70086957")
        assert funding.last_funding_rate == Decimal("0.00007528")
        # 0.007528% per interval = 0.7528 bps
        assert funding.last_funding_rate_bps == pytest.approx(
            Decimal("0.7528"), abs=Decimal("0.0001")
        )

    def test_next_funding_time_is_parsed(self) -> None:
        funding = parse_funding(load("futures_premium_index"), PERP, NOW)
        assert funding.next_funding_time.tzinfo is UTC

    def test_missing_rate_raises(self) -> None:
        payload = load("futures_premium_index")
        del payload["lastFundingRate"]
        with pytest.raises(ExchangeDataError, match="lastFundingRate"):
            parse_funding(payload, PERP, NOW)
