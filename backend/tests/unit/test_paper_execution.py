"""The paper simulator: every way an order fails to become a complete fill.

The point of these tests is the disappointing outcomes. A simulator that can
only report "filled" cannot tell us whether the strategy works, so most of
what follows checks that it refuses, partially fills, or expires when the
book says it should - and that each refusal names itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.core.config import CostsConfig, ExecutionConfig
from trading_bot.db.models.enums import MarketType, OrderStatus, OrderType, Side, TimeInForce
from trading_bot.exchange.models import BookLevel, MarketRef, MarketSpec, OrderBook, Quote
from trading_bot.execution.models import OrderRequest, RejectionCode
from trading_bot.execution.paper import PaperExecutionAdapter
from trading_bot.marketdata.models import BookStatus, FeedStatus, MarketSnapshot
from trading_bot.strategy.fees import FeeSchedule

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
SPOT = MarketRef("binance", "BTCUSDT", MarketType.SPOT)
FEES = FeeSchedule.from_config(CostsConfig())


class Clock:
    """A clock the test moves, so latency is deterministic."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += timedelta(milliseconds=ms)


class Feed:
    """Stands in for the engine: whatever market state the test sets.

    ``current`` is mutable so a test can move the market *during* an order,
    which is what the latency delay exists to allow.
    """

    def __init__(self, current: MarketSnapshot) -> None:
        self.current = current
        self.reads = 0

    def snapshot(self, ref: MarketRef) -> MarketSnapshot:
        self.reads += 1
        return self.current


def book(
    bids: tuple[tuple[str, str], ...] = (("99.99", "10"),),
    asks: tuple[tuple[str, str], ...] = (("100.01", "10"),),
) -> OrderBook:
    return OrderBook(
        ref=SPOT,
        bids=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in bids),
        asks=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in asks),
        local_timestamp=NOW,
        sequence=42,
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
            bid=Decimal("99.99"),
            ask=Decimal("100.01"),
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


async def _no_sleep(_seconds: float) -> None:
    """Latency without waiting: the test decides when the book changes."""
    return None


def adapter(
    feed: Feed,
    *,
    spec: MarketSpec | None = None,
    clock: Clock | None = None,
    sleep: object = _no_sleep,
    average_prices: Callable[[MarketRef], Awaitable[Decimal]] | None = None,
    **overrides: object,
) -> PaperExecutionAdapter:
    config = ExecutionConfig(enabled=True, **overrides)  # type: ignore[arg-type]
    return PaperExecutionAdapter(
        feed,
        config,
        fees=FEES,
        specs=(lambda _ref: spec) if spec is not None else None,
        average_prices=average_prices,
        clock=clock or Clock(),
        sleep=sleep,  # type: ignore[arg-type]
    )


def market(quantity: str = "1", expected: str = "100.01", **kwargs: object) -> OrderRequest:
    return OrderRequest(
        ref=SPOT,
        side=Side.BUY,
        quantity=Decimal(quantity),
        expected_price=Decimal(expected),
        **kwargs,  # type: ignore[arg-type]
    )


def spec_for(**overrides: object) -> MarketSpec:
    defaults: dict[str, object] = {
        "ref": SPOT,
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "is_active": True,
    }
    return MarketSpec(**{**defaults, **overrides})  # type: ignore[arg-type]


class TestMarketOrders:
    async def test_a_buy_crosses_to_the_ask_never_the_mid(self) -> None:
        """Filling at the mid is the simplest way to invent an edge."""
        result = await adapter(Feed(snapshot())).submit(market())
        assert result.status is OrderStatus.FILLED
        assert result.average_price == Decimal("100.01")
        assert result.average_price != Decimal("100")  # the mid

    async def test_a_sell_crosses_to_the_bid(self) -> None:
        request = OrderRequest(ref=SPOT, side=Side.SELL, quantity=Decimal(1))
        result = await adapter(Feed(snapshot())).submit(request)
        assert result.average_price == Decimal("99.99")

    async def test_size_walks_the_book_level_by_level(self) -> None:
        depth = book(asks=(("100.01", "1"), ("100.05", "1")))
        result = await adapter(Feed(snapshot(depth))).submit(market("2"))
        assert result.status is OrderStatus.FILLED
        assert result.average_price == Decimal("100.03")

    async def test_depth_that_runs_out_is_a_partial_fill_not_a_fill(self) -> None:
        """The single most flattering lie available: reporting this as filled."""
        depth = book(asks=(("100.01", "0.4"),))
        result = await adapter(Feed(snapshot(depth))).submit(market("1"))
        assert result.status is OrderStatus.PARTIALLY_FILLED
        assert result.filled_quantity == Decimal("0.4")
        assert not result.is_complete
        assert result.rejection is RejectionCode.INSUFFICIENT_DEPTH

    async def test_exhausting_a_bounded_snapshot_is_unknown_not_zero_liquidity(self) -> None:
        depth = OrderBook(
            ref=SPOT,
            bids=(BookLevel(Decimal("99.99"), Decimal(10)),),
            asks=(BookLevel(Decimal("100.01"), Decimal("0.4")),),
            local_timestamp=NOW,
            sequence=42,
            asks_complete=False,
        )
        result = await adapter(Feed(snapshot(depth))).submit(market("1"))
        assert result.status is OrderStatus.PARTIALLY_FILLED
        assert result.rejection is RejectionCode.DEPTH_TRUNCATED

    async def test_an_unsynchronised_book_fills_nothing(self) -> None:
        feed = Feed(snapshot(book_status=BookStatus.SYNCING))
        result = await adapter(feed).submit(market())
        assert result.status is OrderStatus.REJECTED
        assert result.rejection is RejectionCode.BOOK_NOT_SYNCED
        assert result.fills == ()

    async def test_a_market_that_is_not_live_fills_nothing(self) -> None:
        result = await adapter(Feed(snapshot(status=FeedStatus.STALE))).submit(market())
        assert result.rejection is RejectionCode.STALE_MARKET_DATA

    async def test_a_stale_book_cannot_price_a_fill(self) -> None:
        """Filling against a price that may no longer exist is a fabrication."""
        feed = Feed(snapshot(book_age_ms=9000))
        result = await adapter(feed, max_book_age_ms=2000).submit(market())
        assert result.status is OrderStatus.REJECTED
        assert result.rejection is RejectionCode.STALE_MARKET_DATA


class TestLatency:
    async def test_the_fill_uses_the_book_as_it_is_after_the_delay(self) -> None:
        """The market does not wait for our order, and neither does this.

        The strategy decided against an ask of 100.01. The book moves while
        the order is in flight, and the fill pays what is there when it
        arrives - 100.20 - not what the decision saw. A simulator handed the
        decision's book models a market that politely holds still.
        """
        feed = Feed(snapshot())

        async def market_moves(_seconds: float) -> None:
            feed.current = snapshot(book(asks=(("100.20", "10"),)))

        result = await adapter(feed, sleep=market_moves).submit(market(expected="100.01"))
        assert result.average_price == Decimal("100.20")
        # ...and the realised slippage against the decision's price says so.
        assert result.slippage_bps is not None and result.slippage_bps > 0

    async def test_realised_slippage_is_signed_not_floored(self) -> None:
        """A fill better than expected must be visible as better.

        The cost model floors slippage at zero because it is charging a cost.
        Measuring execution is the opposite job: the distribution of the error
        in both directions is what says whether the model can be trusted.
        """
        better = snapshot(book(bids=(("99.40", "10"),), asks=(("99.50", "10"),)))
        result = await adapter(Feed(better)).submit(market(expected="100.01"))
        assert result.slippage_bps is not None and result.slippage_bps < 0


class TestVenueFilters:
    async def test_below_the_minimum_quantity_is_refused(self) -> None:
        feed = Feed(snapshot())
        spec = spec_for(min_qty=Decimal("0.01"), step_size=Decimal("0.001"))
        result = await adapter(feed, spec=spec).submit(market("0.005"))
        assert result.status is OrderStatus.REJECTED
        assert result.rejection is RejectionCode.BELOW_MIN_QUANTITY
        assert feed.reads == 0  # refused before the book was even read

    async def test_market_notional_uses_the_venue_average_when_required(self) -> None:
        async def average_price(_ref: MarketRef) -> Decimal:
            return Decimal("10")

        spec = spec_for(
            min_notional=Decimal("50"),
            notional_avg_price_mins=5,
        )
        result = await adapter(Feed(snapshot()), spec=spec, average_prices=average_price).submit(
            market("4")
        )
        assert result.status is OrderStatus.REJECTED
        assert result.rejection is RejectionCode.BELOW_MIN_NOTIONAL
        assert "40.00" in (result.detail or "")

    async def test_market_notional_refuses_when_required_average_is_unavailable(self) -> None:
        spec = spec_for(
            min_notional=Decimal("50"),
            notional_avg_price_mins=5,
        )
        result = await adapter(Feed(snapshot()), spec=spec).submit(market("1"))
        assert result.status is OrderStatus.REJECTED
        assert result.rejection is RejectionCode.NOTIONAL_REFERENCE_UNAVAILABLE

    async def test_above_the_maximum_quantity_is_refused(self) -> None:
        """MARKET_LOT_SIZE is the binding cap on every USD-M perpetual."""
        spec = spec_for(max_qty=Decimal(1000), market_max_qty=Decimal(120))
        result = await adapter(Feed(snapshot()), spec=spec).submit(market("200"))
        assert result.rejection is RejectionCode.ABOVE_MAX_QUANTITY

    async def test_market_lot_limits_do_not_apply_to_a_limit_order(self) -> None:
        spec = spec_for(
            max_qty=Decimal(1000),
            market_max_qty=Decimal(1),
            step_size=Decimal("0.1"),
        )
        request = OrderRequest(
            ref=SPOT,
            side=Side.BUY,
            quantity=Decimal(2),
            order_type=OrderType.LIMIT,
            price=Decimal("100.01"),
            time_in_force=TimeInForce.IOC,
        )
        result = await adapter(Feed(snapshot()), spec=spec).submit(request)
        assert result.status is OrderStatus.FILLED

    async def test_a_quantity_off_the_step_is_refused(self) -> None:
        spec = spec_for(step_size=Decimal("0.001"))
        result = await adapter(Feed(snapshot()), spec=spec).submit(market("0.0015"))
        assert result.rejection is RejectionCode.INVALID_STEP_SIZE

    async def test_below_the_minimum_notional_is_refused(self) -> None:
        """The perpetual minimum used to parse as None, so nothing caught this."""
        spec = spec_for(min_notional=Decimal(50), step_size=Decimal("0.001"))
        result = await adapter(Feed(snapshot()), spec=spec).submit(market("0.01"))
        assert result.rejection is RejectionCode.BELOW_MIN_NOTIONAL

    async def test_a_price_off_the_tick_is_refused(self) -> None:
        spec = spec_for(tick_size=Decimal("0.01"))
        request = OrderRequest(
            ref=SPOT,
            side=Side.BUY,
            quantity=Decimal(1),
            order_type=OrderType.LIMIT,
            price=Decimal("100.005"),
            time_in_force=TimeInForce.IOC,
        )
        result = await adapter(Feed(snapshot()), spec=spec).submit(request)
        assert result.rejection is RejectionCode.INVALID_TICK_SIZE

    async def test_a_market_that_is_not_trading_is_refused(self) -> None:
        result = await adapter(Feed(snapshot()), spec=spec_for(is_active=False)).submit(market())
        assert result.rejection is RejectionCode.MARKET_NOT_TRADING

    async def test_no_spec_means_no_filter_check_not_an_invented_one(self) -> None:
        result = await adapter(Feed(snapshot())).submit(market("0.00001"))
        assert result.status is OrderStatus.FILLED


class TestRestingOrders:
    """Whether a maker order fills is the assumption Phase 6 had to make."""

    def _limit(
        self, price: str, quantity: str = "1", tif: TimeInForce = TimeInForce.IOC
    ) -> OrderRequest:
        return OrderRequest(
            ref=SPOT,
            side=Side.BUY,
            quantity=Decimal(quantity),
            order_type=OrderType.LIMIT,
            price=Decimal(price),
            time_in_force=tif,
            expected_price=Decimal(price),
        )

    async def test_a_limit_at_the_touch_crosses_as_a_taker_not_a_maker(self) -> None:
        """The trap: an order priced through the touch does not rest at all.

        A buy limit at or above the best ask fills immediately, exactly as a
        market order would. Calling that a maker fill hands the cost model
        the cheaper rate for an order that crossed the spread - the discount
        this codebase refuses to award itself.
        """
        feed = Feed(snapshot(book(asks=(("100.00", "5"),))))
        result = await adapter(feed).submit(self._limit("100.01"))
        assert result.status is OrderStatus.FILLED
        assert result.fills[0].is_maker is False
        # Filled at the book's price, not at our limit.
        assert result.average_price == Decimal("100.00")

    async def test_gtc_is_refused_without_trade_prints_and_queue_position(self) -> None:
        """Book movement alone is not evidence that our resting order filled."""
        feed = Feed(snapshot(book(asks=(("100.50", "5"),))))
        result = await adapter(feed).submit(self._limit("100.00", tif=TimeInForce.GTC))
        assert result.status is OrderStatus.REJECTED
        assert result.rejection is RejectionCode.UNSUPPORTED_TIME_IN_FORCE
        assert result.fills == ()

    async def test_a_limit_the_market_never_reaches_expires_unfilled(self) -> None:
        """IOC is decided from one arrival-time book and never rests."""
        feed = Feed(snapshot(book(asks=(("100.50", "5"),))))
        result = await adapter(feed).submit(self._limit("100.00"))
        assert result.status is OrderStatus.EXPIRED
        assert result.rejection is RejectionCode.INSUFFICIENT_DEPTH
        assert result.fills == ()
        assert feed.reads == 1

    async def test_a_marketable_limit_takes_only_what_is_inside_its_price(self) -> None:
        """It crosses, empties the levels inside the limit, and stops there."""
        depth = book(asks=(("100.00", "0.3"), ("100.90", "5")))
        result = await adapter(Feed(snapshot(depth))).submit(self._limit("100.00", "1"))
        assert result.status is OrderStatus.PARTIALLY_FILLED
        assert result.filled_quantity == Decimal("0.3")
        assert result.fills[0].is_maker is False
        assert result.rejection is RejectionCode.INSUFFICIENT_DEPTH

    async def test_ioc_partial_fill_cancels_the_remainder(self) -> None:
        feed = Feed(snapshot(book(asks=(("100.00", "0.3"), ("100.50", "5")))))
        result = await adapter(feed).submit(self._limit("100.00", "1"))
        assert result.status is OrderStatus.PARTIALLY_FILLED
        assert result.filled_quantity == Decimal("0.3")
        assert result.fills[0].is_maker is False
        assert result.rejection is RejectionCode.INSUFFICIENT_DEPTH
        assert result.is_terminal

    async def test_a_resting_order_can_be_cancelled(self) -> None:
        instance = adapter(Feed(snapshot(book(asks=(("100.50", "5"),)))))
        request = self._limit("100.00")
        await instance.cancel(request.client_order_id)
        result = await instance.submit(request)
        assert result.status is OrderStatus.CANCELLED
        assert result.rejection is RejectionCode.CANCELLED_BY_CALLER

    async def test_cancelling_a_finished_order_is_refused(self) -> None:
        instance = adapter(Feed(snapshot()))
        request = market()
        await instance.submit(request)
        ack = await instance.cancel(request.client_order_id)
        assert ack.cancelled is False
        assert ack.detail is not None and "FILLED" in ack.detail


class TestIdempotencyAndTimeouts:
    async def test_resubmitting_the_same_id_does_not_execute_twice(self) -> None:
        """A retry after a timeout must not open a second position."""
        instance = adapter(Feed(snapshot()))
        request = market()
        first = await instance.submit(request)
        second = await instance.submit(request)
        assert second is first
        assert len(instance.orders) == 1

    async def test_concurrent_duplicate_submissions_share_one_simulation(self) -> None:
        entered = 0
        release = asyncio.Event()

        async def paused(_seconds: float) -> None:
            nonlocal entered
            entered += 1
            await release.wait()

        instance = adapter(Feed(snapshot()), sleep=paused)
        request = market()
        first = asyncio.create_task(instance.submit(request))
        second = asyncio.create_task(instance.submit(request))
        await asyncio.sleep(0)
        release.set()
        one, two = await asyncio.gather(first, second)
        assert one is two
        assert entered == 1

    async def test_an_order_with_no_terminal_state_in_time_fails(self) -> None:
        """We do not know what the venue did with it, so it is not 'nothing'."""

        async def slow(seconds: float) -> Awaitable[None]:
            import asyncio

            await asyncio.sleep(max(seconds, 0.05))
            return None

        feed = Feed(snapshot(book(asks=(("100.50", "5"),))))
        instance = adapter(
            feed,
            sleep=slow,
            timeout_ms=10,
            latency_ms=5,
            latency_jitter_ms=0,
        )
        result = await instance.submit(market())
        assert result.status is OrderStatus.FAILED
        assert result.rejection is RejectionCode.TIMEOUT

    async def test_status_answers_for_an_order_it_has_seen(self) -> None:
        instance = adapter(Feed(snapshot()))
        request = market()
        await instance.submit(request)
        assert await instance.status(request.client_order_id) is not None
        assert await instance.status("never-submitted") is None


class TestFees:
    async def test_a_market_order_pays_the_taker_rate(self) -> None:
        result = await adapter(Feed(snapshot())).submit(market())
        fill = result.fills[0]
        assert fill.is_maker is False
        assert fill.fee_usd == fill.notional * Decimal(10) / Decimal(10_000)

    async def test_a_simulated_order_id_says_it_is_simulated(self) -> None:
        """Prefixed so nobody mistakes it for something a venue issued."""
        result = await adapter(Feed(snapshot())).submit(market())
        assert result.exchange_order_id is not None
        assert result.exchange_order_id.startswith("paper-")

    async def test_the_fill_records_the_book_it_was_priced_against(self) -> None:
        """A paper fill has to be re-derivable, like every other stored decision."""
        result = await adapter(Feed(snapshot())).submit(market())
        assert result.book_sequence == 42
        assert result.book_local_timestamp == NOW


def test_an_order_for_nothing_is_not_an_order() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        OrderRequest(ref=SPOT, side=Side.BUY, quantity=Decimal(0))


def test_a_limit_order_without_a_price_is_refused() -> None:
    with pytest.raises(ValueError, match="needs a price"):
        OrderRequest(ref=SPOT, side=Side.BUY, quantity=Decimal(1), order_type=OrderType.LIMIT)
