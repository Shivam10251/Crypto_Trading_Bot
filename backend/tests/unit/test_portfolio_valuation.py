"""Pricing an exit, and refusing to price one.

A valuation that quietly falls back to the last price it saw is worse than no
valuation at all: it looks like a measurement. These tests pin down every case
where there is no honest price, and the executable-depth walk that produces one
when there is.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trading_bot.db.models.enums import MarketType, Side
from trading_bot.exchange.models import BookLevel, MarketRef, OrderBook, Quote
from trading_bot.marketdata.models import BookStatus, FeedStatus, MarketSnapshot
from trading_bot.portfolio.valuation import (
    ExitPricingProblem,
    MarkReader,
    exit_side,
    position_value_usd,
    price_exit,
)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
PERP = MarketRef("binance", "BTCUSDT", MarketType.PERPETUAL)


def book(
    bids: tuple[tuple[str, str], ...] = (("99", "10"),),
    asks: tuple[tuple[str, str], ...] = (("101", "10"),),
    *,
    bids_complete: bool = True,
    asks_complete: bool = True,
) -> OrderBook:
    return OrderBook(
        ref=SPOT,
        bids=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in bids),
        asks=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in asks),
        local_timestamp=NOW,
        sequence=7,
        bids_complete=bids_complete,
        asks_complete=asks_complete,
    )


def snapshot(
    depth: OrderBook | None = None,
    *,
    status: FeedStatus = FeedStatus.LIVE,
    book_status: BookStatus = BookStatus.SYNCED,
    book_age_ms: int | None = 10,
) -> MarketSnapshot:
    depth = book() if depth is None and book_status is BookStatus.SYNCED else depth
    return MarketSnapshot(
        ref=SPOT,
        status=status,
        quote=Quote(
            ref=SPOT,
            bid=Decimal(99),
            ask=Decimal(101),
            bid_size=Decimal(10),
            ask_size=Decimal(10),
            local_timestamp=NOW,
        ),
        book=depth,
        book_status=book_status,
        last_price=None,
        volume_24h=None,
        quote_volume_24h=None,
        latency_ms=20,
        last_update_at=NOW,
        age_ms=10,
        quote_age_ms=10,
        book_age_ms=book_age_ms if depth else None,
        updates=1,
        gaps=0,
        resyncs=0,
    )


class TestExitSide:
    def test_reducing_a_long_means_selling(self) -> None:
        assert exit_side(Side.BUY) is Side.SELL
        assert exit_side(Side.SELL) is Side.BUY


class TestExecutablePricing:
    def test_a_long_exit_walks_the_bids(self) -> None:
        """Selling 2 into 99 gets 99, not the 100 mid."""
        priced = price_exit(
            SPOT, snapshot(), entry_side=Side.BUY, quantity=Decimal(2), max_book_age_ms=2000
        )

        assert priced.is_priced
        assert priced.price == Decimal(99)
        assert priced.side is Side.SELL

    def test_a_short_exit_walks_the_asks(self) -> None:
        priced = price_exit(
            SPOT, snapshot(), entry_side=Side.SELL, quantity=Decimal(2), max_book_age_ms=2000
        )

        assert priced.price == Decimal(101)
        assert priced.side is Side.BUY

    def test_the_walk_is_a_vwap_across_levels(self) -> None:
        """Selling 3 into 99x1 then 98x5 is (99 + 98*2)/3 = 98.333...."""
        depth = book(bids=(("99", "1"), ("98", "5")))
        priced = price_exit(
            SPOT,
            snapshot(depth),
            entry_side=Side.BUY,
            quantity=Decimal(3),
            max_book_age_ms=2000,
        )

        assert priced.price == Decimal(295) / Decimal(3)
        assert priced.complete


class TestRefusals:
    def test_no_feed_has_no_price(self) -> None:
        priced = price_exit(
            SPOT, None, entry_side=Side.BUY, quantity=Decimal(1), max_book_age_ms=2000
        )

        assert priced.price is None
        assert priced.problem == ExitPricingProblem.NO_FEED

    def test_a_stale_feed_has_no_price(self) -> None:
        priced = price_exit(
            SPOT,
            snapshot(status=FeedStatus.STALE),
            entry_side=Side.BUY,
            quantity=Decimal(1),
            max_book_age_ms=2000,
        )

        assert priced.problem == ExitPricingProblem.NOT_LIVE

    def test_an_unsynced_book_has_no_price(self) -> None:
        priced = price_exit(
            SPOT,
            snapshot(book_status=BookStatus.SYNCING),
            entry_side=Side.BUY,
            quantity=Decimal(1),
            max_book_age_ms=2000,
        )

        assert priced.problem == ExitPricingProblem.BOOK_NOT_SYNCED

    def test_a_book_past_its_age_limit_is_refused_not_reused(self) -> None:
        priced = price_exit(
            SPOT,
            snapshot(book_age_ms=5000),
            entry_side=Side.BUY,
            quantity=Decimal(1),
            max_book_age_ms=2000,
        )

        assert priced.price is None
        assert priced.problem == ExitPricingProblem.STALE_BOOK

    def test_insufficient_depth_prices_what_there_is_and_says_so(self) -> None:
        """A price for part of the size is not a price for the position."""
        priced = price_exit(
            SPOT,
            snapshot(book(bids=(("99", "1"),))),
            entry_side=Side.BUY,
            quantity=Decimal(5),
            max_book_age_ms=2000,
        )

        assert priced.price == Decimal(99)
        assert priced.fillable == Decimal(1)
        assert not priced.complete
        assert not priced.is_priced
        assert priced.problem == ExitPricingProblem.INSUFFICIENT_DEPTH

    def test_a_truncated_snapshot_is_distinguished_from_a_thin_book(self) -> None:
        """ "The book ends here" and "our view ends here" are different facts."""
        priced = price_exit(
            SPOT,
            snapshot(book(bids=(("99", "1"),), bids_complete=False)),
            entry_side=Side.BUY,
            quantity=Decimal(5),
            max_book_age_ms=2000,
        )

        assert priced.problem == ExitPricingProblem.DEPTH_TRUNCATED


class TestMarkReader:
    def test_a_feed_that_raises_becomes_an_unpriceable_exit(self) -> None:
        class Broken:
            def snapshot(self, ref: MarketRef) -> MarketSnapshot:
                raise RuntimeError("engine is gone")

        reader = MarkReader(Broken(), max_book_age_ms=2000)  # type: ignore[arg-type]

        priced = reader.executable_exit(SPOT, entry_side=Side.BUY, quantity=Decimal(1))

        assert priced.price is None
        assert priced.problem == ExitPricingProblem.NO_FEED

    def test_it_passes_the_configured_age_limit_through(self) -> None:
        class Feed:
            def snapshot(self, ref: MarketRef) -> MarketSnapshot:
                return snapshot(book_age_ms=3000)

        reader = MarkReader(Feed(), max_book_age_ms=2000)  # type: ignore[arg-type]

        assert (
            reader.executable_exit(SPOT, entry_side=Side.BUY, quantity=Decimal(1)).problem
            == ExitPricingProblem.STALE_BOOK
        )


class TestPositionValue:
    def test_a_long_spot_holding_is_worth_what_selling_it_fetches(self) -> None:
        value = position_value_usd(
            MarketType.SPOT,
            entry_side=Side.BUY,
            entry_price=Decimal(100),
            quantity=Decimal(2),
            executable_price=Decimal(99),
        )

        assert value == Decimal(198)

    def test_a_borrowed_spot_short_is_a_liability(self) -> None:
        value = position_value_usd(
            MarketType.SPOT,
            entry_side=Side.SELL,
            entry_price=Decimal(100),
            quantity=Decimal(2),
            executable_price=Decimal(101),
        )

        assert value == Decimal(-202)

    def test_a_perpetual_contributes_only_its_unrealized_pnl(self) -> None:
        """Its margin was reserved against cash, not spent, so the notional
        must not be counted again as an asset."""
        long_leg = position_value_usd(
            MarketType.PERPETUAL,
            entry_side=Side.BUY,
            entry_price=Decimal(100),
            quantity=Decimal(2),
            executable_price=Decimal(101),
        )
        short_leg = position_value_usd(
            MarketType.PERPETUAL,
            entry_side=Side.SELL,
            entry_price=Decimal(100),
            quantity=Decimal(2),
            executable_price=Decimal(101),
        )

        assert long_leg == Decimal(2)
        assert short_leg == Decimal(-2)
