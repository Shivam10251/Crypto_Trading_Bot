"""Normalized market-data types: invariants and derived quantities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.errors import ExchangeDataError
from trading_bot.exchange.models import (
    BookLevel,
    Fill,
    MarketRef,
    MarketSpec,
    OrderBook,
    Quote,
    ServerTime,
    combined_step,
    floor_to_step,
)

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
    def test_a_zero_size_snapshot_level_is_not_liquidity(self) -> None:
        with pytest.raises(ExchangeDataError, match="positive finite"):
            OrderBook(
                ref=SPOT,
                bids=(BookLevel(Decimal("100000"), Decimal(0)),),
                asks=(BookLevel(Decimal("100010"), Decimal(1)),),
                local_timestamp=NOW,
            )

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


class TestWalkingABook:
    def test_the_levels_consumed_are_kept_so_a_fill_can_be_re_derived(self) -> None:
        b = book([("100000", "1")], [("100010", "1"), ("100020", "3")])
        fill = b.walk(Side.BUY, Decimal("2"))
        assert fill.is_complete
        # The last level is trimmed to the part actually taken, not copied
        # whole: 1 at 100010 then 1 of the 3 resting at 100020.
        assert fill.levels == (
            BookLevel(Decimal("100010"), Decimal("1")),
            BookLevel(Decimal("100020"), Decimal("1")),
        )
        assert fill.average_price == Decimal("100015")

    def test_a_partial_walk_says_so_rather_than_pricing_what_filled(self) -> None:
        b = book([("100000", "5")], [("100010", "1")])
        fill = b.walk(Side.BUY, Decimal("10"))
        assert not fill.is_complete
        assert fill.filled == Decimal("1")

    def test_an_empty_walk_has_no_price(self) -> None:
        b = book([("100000", "5")], [("100010", "1")])
        fill = OrderBook(ref=b.ref, bids=b.bids, asks=b.asks, local_timestamp=NOW).walk(
            Side.BUY, Decimal("1")
        )
        assert fill.average_price is not None
        empty = Fill(side=Side.BUY, requested=Decimal(1), filled=Decimal(0), levels=())
        assert empty.average_price is None


class TestQuantityIncrements:
    def test_the_common_step_of_two_nested_steps_is_the_coarser_one(self) -> None:
        """BTC spot steps in 0.00001 and its perpetual in 0.001."""
        assert combined_step(Decimal("0.00001"), Decimal("0.001")) == Decimal("0.001")

    def test_a_filter_that_constrains_nothing_is_ignored(self) -> None:
        """Binance publishes stepSize 0 on spot MARKET_LOT_SIZE."""
        assert combined_step(Decimal("0.001"), Decimal(0)) == Decimal("0.001")
        assert combined_step(None, None) is None

    def test_steps_that_do_not_nest_take_their_lowest_common_multiple(self) -> None:
        assert combined_step(Decimal("0.02"), Decimal("0.03")) == Decimal("0.06")

    def test_a_common_step_is_always_exactly_representable(self) -> None:
        """Decimal denominators are powers of ten, so the lcm always terminates."""
        step = combined_step(Decimal("0.00000001"), Decimal("0.003"))
        assert step == Decimal("0.003")
        assert combined_step(Decimal("2E+3"), Decimal("300")) == Decimal(6000)

    def test_flooring_never_rounds_up(self) -> None:
        assert floor_to_step(Decimal("0.0019"), Decimal("0.001")) == Decimal("0.001")
        assert floor_to_step(Decimal("0.0009"), Decimal("0.001")) == Decimal(0)

    def test_no_step_leaves_the_quantity_alone(self) -> None:
        assert floor_to_step(Decimal("0.0019"), None) == Decimal("0.0019")


class TestSpecOrderConstraints:
    def test_the_tighter_of_the_two_lot_filters_wins(self) -> None:
        spec = MarketSpec(
            ref=SPOT,
            base_asset="BTC",
            quote_asset="USDT",
            is_active=True,
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            max_qty=Decimal(1000),
            market_max_qty=Decimal(120),
        )
        assert spec.order_min_qty == Decimal("0.001")
        assert spec.order_max_qty == Decimal(120)
        assert spec.order_step_size == Decimal("0.001")

    def test_a_spec_with_no_filters_constrains_nothing(self) -> None:
        spec = MarketSpec(ref=SPOT, base_asset="BTC", quote_asset="USDT", is_active=True)
        assert spec.order_min_qty is None
        assert spec.order_max_qty is None
        assert spec.order_step_size is None
