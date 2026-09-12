"""The execution boundary's vocabulary: requests, acknowledgements, fills.

These types are what a strategy's intent becomes once it leaves the strategy
layer, and they are deliberately venue-agnostic. ``PaperExecutionAdapter``
speaks them today and ``LiveExecutionAdapter`` will speak the same ones in
Phase 17, which is what lets the mode be a wiring decision rather than a code
change anywhere upstream.

Two things are modelled here that a naive simulator leaves out:

- **An order request is not an order.** It carries a ``client_order_id``
  generated before submission, so a retry after a timeout reuses it and the
  database refuses to create a second order. The idempotency is a constraint,
  not a code path.
- **A result is not a fill.** Every terminal outcome names itself -
  ``FILLED``, ``PARTIALLY_FILLED``, ``REJECTED``, ``EXPIRED``, ``CANCELLED``,
  ``FAILED`` - and every non-fill carries the reason. A simulator that can
  only say "filled" is a simulator that cannot be wrong, which makes it
  useless for deciding whether a strategy works.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trading_bot.db.models.enums import ExecutionMode, OrderStatus, OrderType, Side, TimeInForce
from trading_bot.exchange.models import BPS_SCALE, BookLevel, MarketRef

#: Order states past which nothing more will happen.
TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        # An ExecutionResult is a terminal report. For IOC/market orders any
        # unfilled remainder has already been cancelled by the venue.
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.FAILED,
    }
)


class OrderIntent(StrEnum):
    """Whether this order takes exposure on or gives it back.

    A close is just the opposite side, but calling it one makes a round trip
    legible in the record - and a round trip is exactly what Phase 8 exists
    to measure, since the cost model has only ever been able to assume one.
    """

    OPEN = "OPEN"
    CLOSE = "CLOSE"


class RejectionCode(StrEnum):
    """Why an order did not become a complete fill.

    These are the venue's own refusals plus the ways execution fails short of
    one. Each is produced from something observed - a filter the order breaks,
    depth that is not there, a feed that stopped - never from a coin flip: a
    simulator whose failures are random measures its own random number
    generator rather than the market.
    """

    # Venue filters, now that the remediation reads all of them.
    BELOW_MIN_QUANTITY = "BELOW_MIN_QUANTITY"
    ABOVE_MAX_QUANTITY = "ABOVE_MAX_QUANTITY"
    INVALID_STEP_SIZE = "INVALID_STEP_SIZE"
    BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"
    INVALID_TICK_SIZE = "INVALID_TICK_SIZE"
    BELOW_MIN_PRICE = "BELOW_MIN_PRICE"
    ABOVE_MAX_PRICE = "ABOVE_MAX_PRICE"
    ABOVE_MAX_NOTIONAL = "ABOVE_MAX_NOTIONAL"
    NOTIONAL_REFERENCE_UNAVAILABLE = "NOTIONAL_REFERENCE_UNAVAILABLE"
    PRICE_REFERENCE_UNAVAILABLE = "PRICE_REFERENCE_UNAVAILABLE"
    OUTSIDE_PERCENT_PRICE = "OUTSIDE_PERCENT_PRICE"
    MARKET_NOT_TRADING = "MARKET_NOT_TRADING"
    # Liquidity.
    NO_LIQUIDITY = "NO_LIQUIDITY"
    INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
    DEPTH_TRUNCATED = "DEPTH_TRUNCATED"
    # The feed, at the moment the order would have reached the venue.
    NO_MARKET_DATA = "NO_MARKET_DATA"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    BOOK_NOT_SYNCED = "BOOK_NOT_SYNCED"
    TIMEOUT = "TIMEOUT"
    ADAPTER_ERROR = "ADAPTER_ERROR"
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
    UNSUPPORTED_TIME_IN_FORCE = "UNSUPPORTED_TIME_IN_FORCE"
    CANCELLED_BY_CALLER = "CANCELLED_BY_CALLER"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    EXPOSURE_LIMIT = "EXPOSURE_LIMIT"
    BORROW_UNAVAILABLE = "BORROW_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """An intent to trade one market. One leg, never two.

    A basis trade is two of these. Keeping them separate is what makes leg
    risk visible: one can fill while the other does not, and a request object
    that bundled both legs would have nowhere to express that.
    """

    ref: MarketRef
    side: Side
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET
    intent: OrderIntent = OrderIntent.OPEN
    # None for a market order; the limit for anything else.
    price: Decimal | None = None
    time_in_force: TimeInForce | None = None
    # The price the strategy expected to get. Realised slippage is measured
    # against this, which is the number that says whether the cost model was
    # telling the truth.
    expected_price: Decimal | None = None
    # Generated before submission, so a retry after a timeout reuses it.
    client_order_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    execution_intent_id: str | None = None
    attempt_id: str | None = None
    strategy: str | None = None
    signal_generated_at: datetime | None = None
    signal_expires_at: datetime | None = None
    expected_net_edge_bps: Decimal | None = None
    # Which signal asked for this; carried through to the stored row.
    signal_leg: int | None = None

    def __post_init__(self) -> None:
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError(f"order quantity must be positive for {self.ref}")
        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError(f"a limit order needs a price for {self.ref}")
        if self.order_type is OrderType.MARKET and self.price is not None:
            raise ValueError(f"a market order cannot carry a limit price for {self.ref}")
        if self.order_type is OrderType.MARKET and self.time_in_force is not None:
            raise ValueError(f"a market order cannot carry time-in-force for {self.ref}")
        if self.order_type is OrderType.LIMIT and self.time_in_force is None:
            raise ValueError(f"a limit order needs time-in-force for {self.ref}")
        if self.price is not None and (not self.price.is_finite() or self.price <= 0):
            raise ValueError(f"order price must be positive for {self.ref}")
        if self.expected_price is not None and (
            not self.expected_price.is_finite() or self.expected_price <= 0
        ):
            raise ValueError(f"expected price must be positive for {self.ref}")
        if not self.client_order_id or len(self.client_order_id) > 64:
            raise ValueError("client_order_id must contain 1 to 64 characters")
        if self.execution_intent_id is not None and len(self.execution_intent_id) > 80:
            raise ValueError("execution_intent_id exceeds its durable 80-character limit")
        if self.attempt_id is not None and len(self.attempt_id) > 64:
            raise ValueError("attempt_id exceeds its durable 64-character limit")
        if self.signal_leg is not None and self.signal_leg < 0:
            raise ValueError("signal_leg cannot be negative")
        for name, timestamp in (
            ("signal_generated_at", self.signal_generated_at),
            ("signal_expires_at", self.signal_expires_at),
        ):
            if timestamp is not None and timestamp.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    """One execution against one order.

    ``is_maker`` is not a configuration choice. Current market and IOC paper
    fills are taker fills. A future GTC simulator may set maker only when trade
    prints and queue-position evidence prove that the resting order was hit.
    """

    price: Decimal
    quantity: Decimal
    filled_at: datetime
    is_maker: bool
    fee_usd: Decimal
    fee_asset: str | None = None
    # Against ``OrderRequest.expected_price``; signed so a fill better than
    # expected reads negative rather than being quietly floored at zero.
    slippage_bps: Decimal | None = None
    fee_rate_bps: Decimal | None = None
    levels: tuple[BookLevel, ...] = ()
    book_sequence: int | None = None
    book_local_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("fill price must be positive and finite")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError("fill quantity must be positive and finite")
        if self.filled_at.tzinfo is None:
            raise ValueError("fill timestamp must be timezone-aware")
        if self.book_local_timestamp is not None and self.book_local_timestamp.tzinfo is None:
            raise ValueError("fill book timestamp must be timezone-aware")

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class OrderAck:
    """The venue accepting - or refusing - an order, before any fill."""

    client_order_id: str
    accepted: bool
    acknowledged_at: datetime
    latency_ms: int
    exchange_order_id: str | None = None
    rejection: RejectionCode | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """What became of one order, in full.

    Carries the fills *and* the reason there were not more of them. An order
    that half-filled and expired is a different fact from one that filled, and
    a different fact again from one the venue refused - and only the first of
    those leaves an open position behind.
    """

    request: OrderRequest
    status: OrderStatus
    fills: tuple[SimulatedFill, ...]
    submitted_at: datetime
    acknowledged_at: datetime | None
    closed_at: datetime | None
    latency_ms: int
    terminal_latency_ms: int | None = None
    mode: ExecutionMode = ExecutionMode.PAPER
    exchange_order_id: str | None = None
    rejection: RejectionCode | None = None
    detail: str | None = None
    # The book the order was priced against at FILL time, not at decision
    # time. They differ by the latency, which is the point of simulating it.
    book_sequence: int | None = None
    book_local_timestamp: datetime | None = None
    venue_filters: Mapping[str, str | bool | int | None] | None = None

    @property
    def filled_quantity(self) -> Decimal:
        return sum((fill.quantity for fill in self.fills), Decimal(0))

    @property
    def is_complete(self) -> bool:
        return self.filled_quantity >= self.request.quantity

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def average_price(self) -> Decimal | None:
        """``None`` when nothing filled: there is no price for zero size."""
        filled = self.filled_quantity
        if filled <= 0:
            return None
        return sum((fill.notional for fill in self.fills), Decimal(0)) / filled

    @property
    def fees_usd(self) -> Decimal:
        return sum((fill.fee_usd for fill in self.fills), Decimal(0))

    @property
    def notional(self) -> Decimal:
        return sum((fill.notional for fill in self.fills), Decimal(0))

    @property
    def slippage_bps(self) -> Decimal | None:
        """Realised slippage against what the strategy expected to pay.

        Signed on purpose. Flooring it at zero - as the cost model does when
        charging a cost - would hide every fill that came in better than
        expected, and the distribution of that error is exactly what says
        whether the cost model can be trusted.
        """
        expected = self.request.expected_price
        average = self.average_price
        if expected is None or average is None or expected <= 0:
            return None
        signed = average - expected if self.request.side is Side.BUY else expected - average
        return signed / expected * BPS_SCALE


@dataclass(frozen=True, slots=True)
class CancelAck:
    """The result of asking to cancel. Cancelling a filled order fails."""

    client_order_id: str
    cancelled: bool
    at: datetime
    detail: str | None = None
