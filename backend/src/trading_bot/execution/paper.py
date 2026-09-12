"""Paper execution: a simulator that is allowed to disappoint.

A signal is not a trade. The whole value of this module is that it can return
something other than "filled", and every way it does so is derived from
something observed rather than drawn from a random number generator:

| Effect | Simulated as |
| --- | --- |
| Spread cost | Buy into the asks, sell into the bids - never at the mid |
| Slippage | Walking the real book by size, level by level |
| Latency | A real delay before the book is read, so the market moves first |
| Partial fills | Only what the visible depth supports at that moment |
| Rejections | The venue's own filters, checked against the real `MarketSpec` |
| Cancellations | IOC/FOK remainder cancelled immediately |
| Timeouts | No usable snapshot within the window |
| Zero liquidity | An empty or unsynchronised book - no fill at all |

**Nothing here is random.** A simulator whose rejections come from a coin flip
measures its own seed; the failures modelled here all come from the venue's
published filters or from depth that was genuinely not there. There is no
"fill probability" setting. GTC maker execution is refused because order-book
movement does not reveal trades or this order's queue position.

**The book is read at fill time, not at decision time.** They are different
books, and the difference is the latency. Handing the simulator the book the
strategy decided on would model a market that politely waits.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from trading_bot.core.config import ExecutionConfig
from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import ExecutionMode, OrderStatus, OrderType, Side, TimeInForce
from trading_bot.exchange.models import BPS_SCALE, BookLevel, Fill, MarketRef, MarketSpec, OrderBook
from trading_bot.execution.base import MarketFeed, SpecSource
from trading_bot.execution.models import (
    CancelAck,
    ExecutionResult,
    OrderRequest,
    RejectionCode,
    SimulatedFill,
)
from trading_bot.marketdata.models import BookStatus, MarketSnapshot
from trading_bot.strategy.fees import FeeSchedule, OrderRole

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


Sleeper = Callable[[float], Awaitable[None]]
MarkPriceSource = Callable[[MarketRef], Decimal | None]
AveragePriceSource = Callable[[MarketRef], Awaitable[Decimal]]


class PaperExecutionAdapter:
    """Simulated execution against the live book. Never touches the venue.

    Holds every order it has seen so ``status`` and ``cancel`` can answer for
    them, which is also what lets a retry with the same ``client_order_id``
    return the original result instead of executing twice.
    """

    mode = ExecutionMode.PAPER

    def __init__(
        self,
        feed: MarketFeed,
        config: ExecutionConfig,
        *,
        fees: FeeSchedule,
        specs: SpecSource | None = None,
        mark_prices: MarkPriceSource | None = None,
        average_prices: AveragePriceSource | None = None,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._feed = feed
        self._config = config
        self._fees = fees
        self._specs = specs
        self._mark_prices = mark_prices
        self._average_prices = average_prices
        self._clock = clock
        self._sleep = sleep
        self._orders: OrderedDict[str, ExecutionResult] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[ExecutionResult]] = {}
        self._cancelled: OrderedDict[str, None] = OrderedDict()

    # --- adapter contract -------------------------------------------------

    async def submit(self, request: OrderRequest) -> ExecutionResult:
        """Place one order; return everything that became of it.

        A ``client_order_id`` already seen returns the original result rather
        than executing again. That is the idempotency the safety rules ask
        for: a retry after a timeout must not open a second position.
        """
        existing = self._orders.get(request.client_order_id)
        if existing is not None:
            logger.info("paper.duplicate_submit", client_order_id=request.client_order_id)
            return existing

        task = self._inflight.get(request.client_order_id)
        if task is None:
            # No await occurs between the check and assignment, so this is
            # atomic within the event loop.  Concurrent duplicate submissions
            # share the same task rather than both walking the book.
            task = asyncio.create_task(self._simulate(request))
            self._inflight[request.client_order_id] = task
            task.add_done_callback(self._completion_callback(request.client_order_id))
        else:
            logger.info(
                "paper.concurrent_duplicate_submit", client_order_id=request.client_order_id
            )
        return await asyncio.shield(task)

    async def _simulate(self, request: OrderRequest) -> ExecutionResult:
        """Run one unique simulation.  ``submit`` owns deduplication."""

        submitted_at = self._clock()
        if request.client_order_id in self._cancelled:
            return self._terminal(
                request,
                OrderStatus.CANCELLED,
                submitted_at,
                rejection=RejectionCode.CANCELLED_BY_CALLER,
                detail="cancelled before submission",
            )
        try:
            async with asyncio.timeout(self._config.timeout_ms / 1000):
                # The order takes time to reach the venue, and the market does
                # not wait. Everything below is priced against the book as it
                # is AFTER this delay, which is the point of simulating one.
                await self._sleep(self._simulated_latency_ms(request) / 1000)
                acknowledged_at = self._clock()
                if request.client_order_id in self._cancelled:
                    return self._terminal(
                        request,
                        OrderStatus.CANCELLED,
                        submitted_at,
                        acknowledged_at=acknowledged_at,
                        rejection=RejectionCode.CANCELLED_BY_CALLER,
                        detail="cancelled before acknowledgement",
                    )
                venue_reference = await self._venue_reference(request)
                rejection = self._filter_violation(request, venue_reference)
                if rejection is not None:
                    return self._terminal(
                        request,
                        OrderStatus.REJECTED,
                        submitted_at,
                        acknowledged_at=acknowledged_at,
                        rejection=rejection[0],
                        detail=rejection[1],
                    )
                result = (
                    self._limit(request, submitted_at, acknowledged_at)
                    if request.order_type is OrderType.LIMIT
                    else self._take(request, submitted_at, acknowledged_at)
                )
        except TimeoutError:
            # No terminal answer within the window. Treated as FAILED rather
            # than as a rejection: we do not know what the venue did with it,
            # and assuming "nothing" is how a real system loses track of a
            # live order.
            result = self._terminal(
                request,
                OrderStatus.FAILED,
                submitted_at,
                rejection=RejectionCode.TIMEOUT,
                detail=f"no terminal state within {self._config.timeout_ms} ms",
            )
        return result

    async def cancel(self, client_order_id: str) -> CancelAck:
        """Withdraw a resting order; refuse if it has already finished."""
        at = self._clock()
        result = self._orders.get(client_order_id)
        if result is not None and result.is_terminal:
            return CancelAck(
                client_order_id=client_order_id,
                cancelled=False,
                at=at,
                detail=f"already {result.status.value}",
            )
        self._cancelled[client_order_id] = None
        self._cancelled.move_to_end(client_order_id)
        while len(self._cancelled) > self._config.max_cached_orders:
            self._cancelled.popitem(last=False)
        return CancelAck(client_order_id=client_order_id, cancelled=True, at=at)

    async def status(self, client_order_id: str) -> ExecutionResult | None:
        return self._orders.get(client_order_id)

    @property
    def orders(self) -> tuple[ExecutionResult, ...]:
        """Every order this adapter has simulated, in submission order."""
        return tuple(self._orders.values())

    # --- venue filters ----------------------------------------------------

    def _simulated_latency_ms(self, request: OrderRequest) -> int:
        """Configured venue latency plus reproducible per-order jitter."""
        base = self._config.latency_ms_by_market_type.get(
            request.ref.market_type.value, self._config.latency_ms
        )
        jitter = self._config.latency_jitter_ms
        if jitter <= 0:
            return base
        digest = hashlib.sha256(request.client_order_id.encode("utf-8")).digest()
        return base + int.from_bytes(digest[:2], "big") % (jitter + 1)

    async def _venue_reference(self, request: OrderRequest) -> Decimal | None:
        spec = self._specs(request.ref) if self._specs is not None else None
        if spec is None:
            return None
        if spec.market_notional_uses_mark_price:
            return self._mark_prices(request.ref) if self._mark_prices is not None else None
        needs_average = bool(
            spec.notional_avg_price_mins
            or spec.percent_price_avg_mins
            or spec.percent_price_up
            or spec.bid_percent_price_up
            or spec.ask_percent_price_up
        )
        if needs_average and self._average_prices is not None:
            try:
                return await self._average_prices(request.ref)
            except Exception as exc:
                logger.warning(
                    "paper.average_price_failed", market=str(request.ref), error=str(exc)
                )
        return None

    def _filter_violation(
        self, request: OrderRequest, venue_reference: Decimal | None
    ) -> tuple[RejectionCode, str] | None:
        """The venue's own refusals, checked before anything is priced.

        These are real filters read from ``exchangeInfo``, which is only
        possible because the remediation made the parser read all of them -
        the perpetual minimum notional in particular used to come back None,
        so an order under it passed every check we had.
        """
        spec = self._specs(request.ref) if self._specs is not None else None
        if spec is None:
            return None  # nothing to check against; not a licence to invent one
        if not spec.is_active:
            return (RejectionCode.MARKET_NOT_TRADING, f"{spec.symbol} is not trading")

        quantity = request.quantity
        minimum = spec.minimum_quantity(request.order_type)
        if minimum is not None and quantity < minimum:
            return (
                RejectionCode.BELOW_MIN_QUANTITY,
                f"{quantity} < venue minimum {minimum}",
            )
        maximum = spec.maximum_quantity(request.order_type)
        if maximum is not None and quantity > maximum:
            return (
                RejectionCode.ABOVE_MAX_QUANTITY,
                f"{quantity} > venue maximum {maximum}",
            )
        step = spec.quantity_step(request.order_type)
        if step is not None and step > 0 and quantity % step != 0:
            return (
                RejectionCode.INVALID_STEP_SIZE,
                f"{quantity} is not a multiple of {step}",
            )
        if request.price is not None:
            if spec.min_price is not None and request.price < spec.min_price:
                return (RejectionCode.BELOW_MIN_PRICE, f"{request.price} < {spec.min_price}")
            if spec.max_price is not None and request.price > spec.max_price:
                return (RejectionCode.ABOVE_MAX_PRICE, f"{request.price} > {spec.max_price}")
            if spec.tick_size and request.price % spec.tick_size != 0:
                return (
                    RejectionCode.INVALID_TICK_SIZE,
                    f"{request.price} is not a multiple of {spec.tick_size}",
                )
            percent_violation = self._percent_price_violation(request, spec, venue_reference)
            if percent_violation is not None:
                return percent_violation
        return self._notional_violation(request, spec, venue_reference)

    def _percent_price_violation(
        self,
        request: OrderRequest,
        spec: MarketSpec,
        venue_reference: Decimal | None,
    ) -> tuple[RejectionCode, str] | None:
        if request.side is Side.BUY:
            up = spec.bid_percent_price_up or spec.percent_price_up
            down = spec.bid_percent_price_down or spec.percent_price_down
        else:
            up = spec.ask_percent_price_up or spec.percent_price_up
            down = spec.ask_percent_price_down or spec.percent_price_down
        if up is None and down is None:
            return None
        reference = venue_reference
        if reference is None:
            return (
                RejectionCode.PRICE_REFERENCE_UNAVAILABLE,
                f"percent-price filter needs venue average over {spec.percent_price_avg_mins} min",
            )
        assert request.price is not None
        if (up is not None and request.price > reference * up) or (
            down is not None and request.price < reference * down
        ):
            return (
                RejectionCode.OUTSIDE_PERCENT_PRICE,
                f"{request.price} outside venue band around {reference}",
            )
        return None

    def _notional_violation(
        self,
        request: OrderRequest,
        spec: MarketSpec,
        venue_reference: Decimal | None,
    ) -> tuple[RejectionCode, str] | None:
        """Minimum notional, priced at whatever reference we honestly have."""
        floor = spec.minimum_notional(request.order_type)
        ceiling = spec.maximum_notional(request.order_type)
        if floor is None and ceiling is None:
            return None
        reference = request.price
        if request.order_type is OrderType.MARKET and (
            spec.market_notional_uses_mark_price or spec.notional_avg_price_mins > 0
        ):
            reference = venue_reference
            if reference is None:
                return (
                    RejectionCode.NOTIONAL_REFERENCE_UNAVAILABLE,
                    "venue reference unavailable for MARKET notional validation",
                )
        if reference is None:
            snapshot = self._safe_snapshot(request)
            reference = None if snapshot is None else snapshot.mid_price
        if reference is None:
            return None  # no price to judge it by; refusing here would be a guess
        notional = reference * request.quantity
        if floor is not None and notional < floor:
            return (
                RejectionCode.BELOW_MIN_NOTIONAL,
                f"{notional:.2f} < venue minimum {floor}",
            )
        if ceiling is not None and notional > ceiling:
            return (
                RejectionCode.ABOVE_MAX_NOTIONAL,
                f"{notional:.2f} > venue maximum {ceiling}",
            )
        return None

    # --- market orders ----------------------------------------------------

    def _take(
        self, request: OrderRequest, submitted_at: datetime, acknowledged_at: datetime
    ) -> ExecutionResult:
        """Cross the spread for whatever the book will give us."""
        book, problem = self._tradeable_book(request)
        if book is None:
            assert problem is not None
            return self._terminal(
                request,
                OrderStatus.REJECTED,
                submitted_at,
                acknowledged_at=acknowledged_at,
                rejection=problem[0],
                detail=problem[1],
            )

        fill = book.walk(request.side, request.quantity)
        if fill.filled <= 0:
            return self._terminal(
                request,
                OrderStatus.REJECTED,
                submitted_at,
                acknowledged_at=acknowledged_at,
                rejection=RejectionCode.NO_LIQUIDITY,
                detail=f"nothing resting on the {_opposite(request.side)} side",
                book=book,
            )

        simulated = self._fill_from(request, fill, taker=True, book=book)
        if fill.is_complete:
            return self._terminal(
                request,
                OrderStatus.FILLED,
                submitted_at,
                acknowledged_at=acknowledged_at,
                fills=(simulated,),
                book=book,
            )
        # A market order takes what is there and the rest simply does not
        # happen. Reporting it as filled would be the single most flattering
        # lie this module could tell.
        return self._terminal(
            request,
            OrderStatus.PARTIALLY_FILLED,
            submitted_at,
            acknowledged_at=acknowledged_at,
            fills=(simulated,),
            rejection=_depth_problem(book, request.side),
            detail=f"book filled {fill.filled} of {request.quantity}",
            book=book,
        )

    # --- limit orders -----------------------------------------------------

    def _limit(
        self, request: OrderRequest, submitted_at: datetime, acknowledged_at: datetime
    ) -> ExecutionResult:
        """Evaluate IOC/FOK once at arrival; never infer a maker fill.

        This is the measurement Phase 6 could not make, and it has a trap in
        it. A limit order priced at or through the far touch **crosses the
        spread immediately** - the venue fills it as a taker, at the book's
        prices, and charges the taker rate. Calling that a maker fill would
        hand the cost model the cheaper rate for an order that did exactly
        what a market order does, which is precisely the discount this
        codebase refuses to award itself.

        GTC needs the public trade stream plus an explicit queue-position
        model. Until those inputs exist it is rejected rather than awarded a
        favourable fill from displayed depth.
        """
        if request.time_in_force is TimeInForce.GTC:
            return self._terminal(
                request,
                OrderStatus.REJECTED,
                submitted_at,
                acknowledged_at=acknowledged_at,
                rejection=RejectionCode.UNSUPPORTED_TIME_IN_FORCE,
                detail="paper GTC maker fills require trade prints and queue position",
            )
        book, problem = self._tradeable_book(request)
        if book is None:
            assert problem is not None
            return self._terminal(
                request,
                OrderStatus.REJECTED,
                submitted_at,
                acknowledged_at=acknowledged_at,
                rejection=problem[0],
                detail=problem[1],
            )
        limit = _require_price(request)
        fill = _walk_to_limit(book, request.side, request.quantity, limit)
        if request.time_in_force is TimeInForce.FOK and not fill.is_complete:
            return self._terminal(
                request,
                OrderStatus.EXPIRED,
                submitted_at,
                acknowledged_at=acknowledged_at,
                rejection=_depth_problem(book, request.side),
                detail=f"FOK could fill {fill.filled} of {request.quantity}",
                book=book,
            )
        fills = (self._fill_from(request, fill, taker=True, book=book),) if fill.filled > 0 else ()
        status = (
            OrderStatus.FILLED
            if fill.is_complete
            else (OrderStatus.PARTIALLY_FILLED if fills else OrderStatus.EXPIRED)
        )
        return self._terminal(
            request,
            status,
            submitted_at,
            acknowledged_at=acknowledged_at,
            fills=fills,
            rejection=None if fill.is_complete else _depth_problem(book, request.side),
            detail=None
            if fill.is_complete
            else f"IOC filled {fill.filled} of {request.quantity}; remainder cancelled",
            book=book,
        )

    # --- the feed ---------------------------------------------------------

    def _safe_snapshot(self, request: OrderRequest) -> MarketSnapshot | None:
        try:
            execution_snapshot = getattr(self._feed, "execution_snapshot", None)
            if execution_snapshot is not None:
                return cast(MarketSnapshot, execution_snapshot(request.ref))
            return self._feed.snapshot(request.ref)
        except Exception as exc:
            logger.warning("paper.snapshot_failed", market=str(request.ref), error=str(exc))
            return None

    def _tradeable_book(
        self, request: OrderRequest
    ) -> tuple[OrderBook, None] | tuple[None, tuple[RejectionCode, str]]:
        """The book to fill against, or why there is not one.

        Read now, at fill time. A feed that has gone quiet cannot price an
        order, and guessing from the last thing it said is how a simulator
        reports fills at prices that no longer existed.
        """
        snapshot = self._safe_snapshot(request)
        if snapshot is None:
            return None, (RejectionCode.NO_MARKET_DATA, f"no feed for {request.ref}")
        if not snapshot.is_live:
            return None, (
                RejectionCode.STALE_MARKET_DATA,
                f"{request.ref} is {snapshot.status.value}",
            )
        age = snapshot.book_age_ms
        if age is not None and age > self._config.max_book_age_ms:
            return None, (
                RejectionCode.STALE_MARKET_DATA,
                f"book {age} ms old, limit {self._config.max_book_age_ms} ms",
            )
        if snapshot.book_status is not BookStatus.SYNCED or snapshot.book is None:
            return None, (
                RejectionCode.BOOK_NOT_SYNCED,
                f"{request.ref} book is {snapshot.book_status.value}",
            )
        return snapshot.book, None

    # --- assembling a result ---------------------------------------------

    def _fill_from(
        self,
        request: OrderRequest,
        fill: Fill,
        *,
        taker: bool,
        price: Decimal | None = None,
        book: OrderBook | None = None,
    ) -> SimulatedFill:
        average = price if price is not None else fill.average_price
        if average is None:  # pragma: no cover - callers check filled > 0 first
            raise ValueError("cannot build a fill with no price")
        role = OrderRole.TAKER if taker else OrderRole.MAKER
        spec = self._specs(request.ref) if self._specs is not None else None
        rate_bps = self._fees.rate_bps(request.ref, role, spec)
        notional = average * fill.filled
        return SimulatedFill(
            price=average,
            quantity=fill.filled,
            filled_at=self._clock(),
            is_maker=not taker,
            fee_usd=notional * rate_bps / BPS_SCALE,
            fee_asset=(
                "BNB"
                if self._fees.pays_in_bnb
                else spec.quote_asset
                if spec is not None
                else request.ref.symbol[-4:]
                if request.ref.symbol.endswith("USDT")
                else None
            ),
            slippage_bps=_slippage_bps(request, average),
            fee_rate_bps=rate_bps,
            levels=fill.levels,
            book_sequence=book.sequence if book is not None else None,
            book_local_timestamp=book.local_timestamp if book is not None else None,
        )

    def _terminal(
        self,
        request: OrderRequest,
        status: OrderStatus,
        submitted_at: datetime,
        *,
        fills: tuple[SimulatedFill, ...] = (),
        acknowledged_at: datetime | None = None,
        rejection: RejectionCode | None = None,
        detail: str | None = None,
        book: OrderBook | None = None,
    ) -> ExecutionResult:
        closed_at = self._clock()
        acknowledgement = acknowledged_at
        if acknowledgement is None and rejection is not RejectionCode.TIMEOUT:
            acknowledgement = closed_at
        return ExecutionResult(
            request=request,
            status=status,
            fills=fills,
            submitted_at=submitted_at,
            acknowledged_at=acknowledgement,
            closed_at=closed_at,
            latency_ms=(
                int((acknowledgement - submitted_at).total_seconds() * 1000)
                if acknowledgement is not None
                else int((closed_at - submitted_at).total_seconds() * 1000)
            ),
            terminal_latency_ms=int((closed_at - submitted_at).total_seconds() * 1000),
            mode=self.mode,
            # Simulated orders get an id so a stored row looks like any other;
            # it is prefixed so nobody mistakes it for a venue's.
            exchange_order_id=f"paper-{uuid.uuid4().hex[:16]}",
            rejection=rejection,
            detail=detail,
            book_sequence=book.sequence if book else None,
            book_local_timestamp=book.local_timestamp if book else None,
            venue_filters=self._filter_evidence(request),
        )

    def _filter_evidence(self, request: OrderRequest) -> dict[str, str | bool | int | None] | None:
        spec = self._specs(request.ref) if self._specs is not None else None
        if spec is None:
            return None

        def value(item: Decimal | None) -> str | None:
            return str(item) if item is not None else None

        return {
            "order_type": request.order_type.value,
            "min_price": value(spec.min_price),
            "max_price": value(spec.max_price),
            "tick_size": value(spec.tick_size),
            "min_quantity": value(spec.minimum_quantity(request.order_type)),
            "max_quantity": value(spec.maximum_quantity(request.order_type)),
            "quantity_step": value(spec.quantity_step(request.order_type)),
            "min_notional": value(spec.minimum_notional(request.order_type)),
            "max_notional": value(spec.maximum_notional(request.order_type)),
            "notional_avg_price_mins": spec.notional_avg_price_mins,
            "market_notional_uses_mark_price": spec.market_notional_uses_mark_price,
            "percent_price_up": value(spec.percent_price_up),
            "percent_price_down": value(spec.percent_price_down),
            "percent_price_avg_mins": spec.percent_price_avg_mins,
        }

    def _completed(self, order_id: str, task: asyncio.Task[ExecutionResult]) -> None:
        self._inflight.pop(order_id, None)
        if task.cancelled():
            return
        try:
            result = task.result()
        except Exception as exc:  # pragma: no cover - coordinator records the exception
            logger.error("paper.simulation_failed", client_order_id=order_id, error=repr(exc))
            return
        self._orders[order_id] = result
        self._orders.move_to_end(order_id)
        while len(self._orders) > self._config.max_cached_orders:
            self._orders.popitem(last=False)

    def _completion_callback(
        self, order_id: str
    ) -> Callable[[asyncio.Task[ExecutionResult]], None]:
        def completed(task: asyncio.Task[ExecutionResult]) -> None:
            self._completed(order_id, task)

        return completed


def _opposite(side: Side) -> str:
    return "ask" if side is Side.BUY else "bid"


def _depth_problem(book: OrderBook, side: Side) -> RejectionCode:
    complete = book.asks_complete if side is Side.BUY else book.bids_complete
    return RejectionCode.INSUFFICIENT_DEPTH if complete else RejectionCode.DEPTH_TRUNCATED


def _require_price(request: OrderRequest) -> Decimal:
    if request.price is None:  # pragma: no cover - OrderRequest enforces this
        raise ValueError("a limit order needs a price")
    return request.price


def _walk_to_limit(book: OrderBook, side: Side, quantity: Decimal, limit: Decimal) -> Fill:
    """Walk the book but never past the limit price."""
    levels = book.asks if side is Side.BUY else book.bids
    remaining = quantity
    taken: list[BookLevel] = []
    for level in levels:
        if remaining <= 0:
            break
        inside = level.price <= limit if side is Side.BUY else level.price >= limit
        if not inside:
            break
        take = min(remaining, level.size)
        taken.append(BookLevel(price=level.price, size=take))
        remaining -= take
    return Fill(side=side, requested=quantity, filled=quantity - remaining, levels=tuple(taken))


def _slippage_bps(request: OrderRequest, average: Decimal) -> Decimal | None:
    expected = request.expected_price
    if expected is None or expected <= 0:
        return None
    signed = average - expected if request.side is Side.BUY else expected - average
    return signed / expected * BPS_SCALE
