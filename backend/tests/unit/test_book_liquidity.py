"""Liquidity near the mid and market-order slippage, from the whole local book."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_bot.db.models.enums import MarketType
from trading_bot.exchange.models import BookLevel, DepthDiff, MarketRef, OrderBook
from trading_bot.marketdata.order_book import BookSyncError, LocalOrderBook

T0 = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
Levels = list[tuple[str, str]]


def book(bids: Levels, asks: Levels) -> LocalOrderBook:
    local = LocalOrderBook(SPOT, min_levels=1)
    local.load_snapshot(
        OrderBook(
            ref=SPOT,
            bids=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in bids),
            asks=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in asks),
            local_timestamp=T0,
            sequence=100,
        )
    )
    return local


def update(*, bids: Levels = (), asks: Levels = ()) -> DepthDiff:  # type: ignore[assignment]
    return DepthDiff(
        ref=SPOT,
        first_update_id=101,
        final_update_id=101,
        bids=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in bids),
        asks=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in asks),
        local_timestamp=T0,
    )


DEEP = book(
    bids=[("99.5", "2"), ("99", "1"), ("98", "5")],
    asks=[("100.5", "1"), ("101", "3"), ("102", "4")],
)


class TestBand:
    def test_value_within_the_band_is_summed_per_side(self) -> None:
        # mid 100, band 100 bps: bids down to 99 and asks up to 101 count.
        liquidity = DEEP.liquidity(Decimal(100), Decimal(50))
        assert liquidity.bid_notional == Decimal("99.5") * 2 + Decimal(99)
        assert liquidity.ask_notional == Decimal("100.5") + Decimal(101) * 3
        assert liquidity.bid_complete
        assert liquidity.ask_complete

    def test_a_band_beyond_the_snapshot_is_a_lower_bound(self) -> None:
        shallow = book(bids=[("99.5", "2"), ("99.4", "1")], asks=[("100.5", "1"), ("100.6", "1")])
        liquidity = shallow.liquidity(Decimal(100), Decimal(10))
        assert not liquidity.bid_complete
        assert not liquidity.ask_complete

    def test_imbalance_leans_toward_the_heavier_side(self) -> None:
        liquidity = DEEP.liquidity(Decimal(100), Decimal(50))
        total = liquidity.bid_notional + liquidity.ask_notional
        assert liquidity.imbalance == (liquidity.bid_notional - liquidity.ask_notional) / total
        assert liquidity.imbalance < 0  # more value resting on the ask


class TestSlippage:
    def test_a_small_order_fills_at_the_touch(self) -> None:
        liquidity = book(bids=[("99", "10")], asks=[("101", "10")]).liquidity(
            Decimal(10), Decimal(101)
        )
        assert liquidity.buy_slippage_bps == Decimal(100)
        assert liquidity.sell_slippage_bps is not None
        assert abs(liquidity.sell_slippage_bps - 100) < Decimal("1e-20")

    def test_a_larger_order_walks_the_book(self) -> None:
        local = book(bids=[("99", "10")], asks=[("101", "1"), ("103", "1")])
        # 101 at 101 plus 103 at 103: two units for 204, average 102 - 200 bps.
        assert local.liquidity(Decimal(10), Decimal(204)).buy_slippage_bps == Decimal(200)

    def test_an_order_the_known_book_cannot_fill_reports_none(self) -> None:
        local = book(bids=[("99", "10")], asks=[("101", "1")])
        assert local.liquidity(Decimal(10), Decimal(1000)).buy_slippage_bps is None

    def test_levels_are_walked_best_first_whatever_their_arrival_order(self) -> None:
        local = book(bids=[("99", "10")], asks=[("101", "1"), ("103", "1")])
        local.apply(update(asks=[("102", "1")]))
        # 101 then 102: two units for 203, average 101.5 - 150 bps.
        assert local.liquidity(Decimal(10), Decimal(203)).buy_slippage_bps == Decimal(150)


class TestGuards:
    def test_a_crossed_book_is_refused(self) -> None:
        local = book(bids=[("99", "1")], asks=[("101", "1"), ("102", "1")])
        local.apply(update(bids=[("101", "1")]))
        with pytest.raises(BookSyncError, match="crossed"):
            local.liquidity(Decimal(10), Decimal(10))

    def test_no_snapshot_no_liquidity(self) -> None:
        with pytest.raises(BookSyncError, match="no snapshot"):
            LocalOrderBook(SPOT, min_levels=1).liquidity(Decimal(10), Decimal(10))
