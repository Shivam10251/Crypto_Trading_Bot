"""Normalized market-data types: invariants and derived quantities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.errors import ExchangeDataError
from trading_bot.exchange.models import BookLevel, MarketRef, OrderBook, Quote, ServerTime

SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def quote(bid: str = "100000", ask: str = "100010", **kwargs: object) -> Quote:
    defaults: dict[str, object] = {
        "ref": SPOT,
        "bid": Decimal(bid),
        "ask": Decimal(ask),
        "bid_size": Decimal("1"),
        "ask_size": Decimal("2"),
        "local_timestamp": NOW,
    }
    return Quote(**{**defaults, **kwargs})  # type: ignore[arg-type]


def book(bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> OrderBook:
    return OrderBook(
        ref=SPOT,
        bids=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in bids),
        asks=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in asks),
        local_timestamp=NOW,
    )


class TestQuoteInvariants:
    """A malformed quote must be impossible to construct."""

    @pytest.mark.parametrize(("bid", "ask"), [("0", "100010"), ("-1", "100010")])
    def test_non_positive_prices_rejected(self, bid: str, ask: str) -> None:
        with pytest.raises(ExchangeDataError, match="non-positive price"):
            quote(bid, ask)

    def test_crossed_book_rejected(self) -> None:
        with pytest.raises(ExchangeDataError, match="crossed book"):
            quote("100020", "100010")

    def test_locked_book_allowed(self) -> None:
        """bid == ask is unusual but real; only crossing is invalid."""
        assert quote("100000", "100000").spread == 0

    def test_negative_size_rejected(self) -> None:
        with pytest.raises(ExchangeDataError, match="negative size"):
            quote(bid_size=Decimal("-1"))

    def test_zero_size_allowed(self) -> None:
        """A price with no size is a real venue state, not corruption."""
        assert quote(bid_size=Decimal("0")).bid_size == 0

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ExchangeDataError, match="timezone-aware"):
            quote(local_timestamp=datetime(2026, 9, 11, 12, 0))


class TestQuoteDerivations:
    def test_mid_and_spread(self) -> None:
        q = quote("100000", "100010")
        assert q.mid_price == Decimal("100005")
        assert q.spread == Decimal("10")

    def test_spread_bps(self) -> None:
        q = quote("100000", "100010")
        # 10 / 100005 * 10_000 ~= 0.9999 bps
        assert q.spread_bps == pytest.approx(Decimal("0.99995"), abs=Decimal("0.0001"))

    def test_latency_is_none_without_an_exchange_clock(self) -> None:
        """Binance spot sends no event time; latency must not be invented."""
        assert quote().latency_ms is None

    def test_latency_measured_when_clock_present(self) -> None:
        q = quote(exchange_timestamp=NOW - timedelta(milliseconds=42))
        assert q.latency_ms == 42

    def test_age_uses_our_own_clock(self) -> None:
        """Staleness is judged on the clock the venue cannot control."""
        assert quote().age_ms(now=NOW + timedelta(milliseconds=500)) == 500

    def test_quotes_are_immutable(self) -> None:
        with pytest.raises(AttributeError):
            quote().bid = Decimal("1")  # type: ignore[misc]


class TestOrderBookInvariants:
    def test_empty_side_rejected(self) -> None:
        with pytest.raises(ExchangeDataError, match="empty book side"):
            OrderBook(ref=SPOT, bids=(), asks=(), local_timestamp=NOW)

    def test_crossed_book_rejected(self) -> None:
        with pytest.raises(ExchangeDataError, match="crossed book"):
            book([("100020", "1")], [("100010", "1")])

    def test_unsorted_bids_rejected(self) -> None:
        """Fills walk these lists in order, so ordering is not cosmetic."""
        with pytest.raises(ExchangeDataError, match="bids not descending"):
            book([("100000", "1"), ("100005", "1")], [("100010", "1")])

    def test_unsorted_asks_rejected(self) -> None:
        with pytest.raises(ExchangeDataError, match="asks not ascending"):
            book([("100000", "1")], [("100015", "1"), ("100010", "1")])


class TestOrderBookAnalytics:
    def test_best_prices_and_mid(self) -> None:
        b = book([("100000", "1"), ("99999", "2")], [("100010", "1"), ("100011", "3")])
        assert b.best_bid == Decimal("100000")
        assert b.best_ask == Decimal("100010")
        assert b.mid_price == Decimal("100005")

    def test_depth_notional_sums_one_side(self) -> None:
        b = book([("100000", "1"), ("99999", "2")], [("100010", "1")])
        assert b.depth_notional(Side.BUY) == Decimal("100000") + Decimal("199998")

    def test_depth_notional_respects_level_limit(self) -> None:
        b = book([("100000", "1"), ("99999", "2")], [("100010", "1")])
        assert b.depth_notional(Side.BUY, levels=1) == Decimal("100000")

    def test_imbalance_is_signed_and_bounded(self) -> None:
        heavy_bid = book([("100000", "10")], [("100010", "1")])
        heavy_ask = book([("100000", "1")], [("100010", "10")])
        assert heavy_bid.imbalance() > Decimal("0.8")
        assert heavy_ask.imbalance() < Decimal("-0.8")
        assert Decimal(-1) <= heavy_bid.imbalance() <= Decimal(1)

    def test_balanced_book_has_near_zero_imbalance(self) -> None:
        b = book([("100000", "1")], [("100000", "1")])
        assert abs(b.imbalance()) < Decimal("0.001")


class TestFillSimulation:
    """This is where slippage stops being a guess."""

    def test_small_order_fills_at_top_of_book(self) -> None:
        b = book([("100000", "5")], [("100010", "5")])
        price, filled = b.fill_price(Side.BUY, Decimal("1"))
        assert price == Decimal("100010")
        assert filled == Decimal("1")

    def test_large_order_walks_the_book(self) -> None:
        b = book([("100000", "5")], [("100010", "1"), ("100020", "1"), ("100030", "1")])
        price, filled = b.fill_price(Side.BUY, Decimal("3"))
        assert filled == Decimal("3")
        # Average of the three levels, worse than the touch.
        assert price == Decimal("100020")
        assert price > b.best_ask

    def test_insufficient_depth_reports_partial_fill(self) -> None:
        """Callers must be able to see they cannot get the size they wanted."""
        b = book([("100000", "5")], [("100010", "1")])
        price, filled = b.fill_price(Side.BUY, Decimal("10"))
        assert filled == Decimal("1")
        assert price == Decimal("100010")

    def test_sell_walks_the_bid_side(self) -> None:
        b = book([("100000", "1"), ("99990", "1")], [("100010", "5")])
        price, filled = b.fill_price(Side.SELL, Decimal("2"))
        assert filled == Decimal("2")
        assert price == Decimal("99995")

    def test_zero_quantity_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            book([("100000", "1")], [("100010", "1")]).fill_price(Side.BUY, Decimal("0"))


class TestServerTime:
    def test_skew_corrects_for_half_the_round_trip(self) -> None:
        clock = ServerTime(
            exchange_time=NOW + timedelta(milliseconds=100),
            local_time=NOW,
            round_trip_ms=40,
        )
        assert clock.skew_ms == 120

    def test_no_skew_reads_as_zero(self) -> None:
        assert ServerTime(exchange_time=NOW, local_time=NOW, round_trip_ms=0).skew_ms == 0
