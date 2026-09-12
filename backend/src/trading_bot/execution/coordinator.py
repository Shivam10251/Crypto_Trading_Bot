"""Turning a two-legged signal into orders, and reporting what really happened.

A basis trade is two orders, and the interesting cases are the ones where they
do not agree. Phase 5 listed leg risk as an unmodelled cost and Phase 6 could
not price it; this is where it stops being a footnote and becomes a measured
outcome:

- **both legs fill** - a hedged position, the only case anyone plans for
- **one fills, the other does not** - naked exposure in one market, which is
  the risk the strategy has always carried and never counted
- **both partially fill, by different amounts** - hedged up to the smaller
  fill and naked for the difference
- **neither fills** - nothing happened, which is a result too

The legs are submitted **concurrently**, because that is how they would be
sent: serialising them would add one leg's latency to the other's and make
the simulation kinder than reality. Nothing here unwinds a half-filled pair -
deciding what to do about naked exposure is the risk engine's job in Phase 9,
and inventing a remedy now would hide how often it happens.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import OrderStatus, OrderType, Side, TimeInForce
from trading_bot.execution.account import AccountRejection, AccountReservation, PaperAccount
from trading_bot.execution.base import ExecutionAdapter, SpecSource
from trading_bot.execution.models import ExecutionResult, OrderIntent, OrderRequest, RejectionCode
from trading_bot.strategy.models import Leg, Signal

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LegOutcome:
    """One leg's order and what became of it."""

    leg: Leg
    result: ExecutionResult

    @property
    def filled(self) -> Decimal:
        return self.result.filled_quantity

    @property
    def is_complete(self) -> bool:
        return self.result.is_complete


@dataclass(frozen=True, slots=True)
class ExecutionAttempt:
    """Both legs of one attempt, and the exposure it actually left behind."""

    # Groups the two orders; stored so a pair can be reassembled from rows.
    attempt_id: str
    buy: LegOutcome
    sell: LegOutcome
    # A probe, not a trade the strategy asked for - see ExecutionConfig.shadow.
    is_shadow: bool = False
    timing_skew_ms: int | None = None
    timing_violation: bool = False

    @property
    def legs(self) -> tuple[LegOutcome, LegOutcome]:
        return (self.buy, self.sell)

    @property
    def is_hedged(self) -> bool:
        """Both legs filled the same size - the only fully hedged outcome."""
        return self.buy.filled > 0 and self.buy.filled == self.sell.filled

    @property
    def unhedged_quantity(self) -> Decimal:
        """Size left naked in one market because the other leg did not match.

        The number the risk engine will have to limit, and the one nothing
        before Phase 8 could produce at all.
        """
        return abs(self.buy.filled - self.sell.filled)

    @property
    def is_empty(self) -> bool:
        return self.buy.filled == 0 and self.sell.filled == 0

    def describe(self) -> str:
        if self.is_empty:
            return "nothing filled"
        if self.is_hedged:
            return f"hedged {self.buy.filled}"
        return (
            f"UNHEDGED {self.unhedged_quantity} (bought {self.buy.filled}, sold {self.sell.filled})"
        )


class ExecutionCoordinator:
    """Submits both legs of a signal and reports the pair's real outcome."""

    def __init__(
        self,
        adapter: ExecutionAdapter,
        *,
        order_type: OrderType = OrderType.MARKET,
        allow_spot_short: bool = False,
        specs: SpecSource | None = None,
        max_leg_skew_ms: int = 250,
        clock: Callable[[], datetime] | None = None,
        account: PaperAccount | None = None,
    ) -> None:
        self._adapter = adapter
        self._order_type = order_type
        # Needed to round a limit to the venue's tick. Measured live: the
        # strategy's executable price is a VWAP of walking the book, which is
        # almost never a multiple of the tick, and the venue rejects it -
        # 10 of 40 limit orders in one run, leaving 8 attempts half-filled
        # and naked because only one leg was refused.
        self._specs = specs
        # The same gate the strategy applies, restated at the boundary that
        # would actually place the order. A cash account cannot sell spot it
        # does not hold, and defending that in one place only is how it
        # eventually gets past.
        self._allow_spot_short = allow_spot_short
        self._max_leg_skew_ms = max_leg_skew_ms
        self._clock = clock or (lambda: datetime.now(UTC))
        self._account = account

    async def execute(
        self,
        signal: Signal,
        *,
        is_shadow: bool = False,
        execution_intent_id: str | None = None,
    ) -> ExecutionAttempt | None:
        """Place both legs at once. ``None`` when the pair is not placeable."""
        opportunity = signal.opportunity
        intent_id = execution_intent_id or uuid.uuid4().hex
        attempt_id = _attempt_id(intent_id)
        requests = [
            self._request(leg, intent_id, attempt_id, index, signal)
            for index, leg in enumerate(opportunity.legs)
        ]
        if opportunity.sell.ref.market_type.value == "SPOT" and (
            not self._allow_spot_short or self._account is None
        ):
            logger.info(
                "execution.spot_short_refused",
                symbol=opportunity.sell.ref.symbol,
                shadow=is_shadow,
            )
            refused = AccountRejection(
                RejectionCode.BORROW_UNAVAILABLE,
                "spot shorting is disabled or no paper account can reserve the borrow",
            )
            return self._attempt(
                attempt_id,
                opportunity.buy,
                opportunity.sell,
                [self._refused(request, refused) for request in requests],
                is_shadow,
            )
        if signal.is_expired(self._clock()):
            results = [self._expired(request) for request in requests]
            return self._attempt(attempt_id, opportunity.buy, opportunity.sell, results, is_shadow)
        reservation: AccountReservation | None = None
        if self._account is not None:
            reserved = await self._account.reserve(signal, intent_id)
            if isinstance(reserved, AccountRejection):
                results = [self._refused(request, reserved) for request in requests]
                return self._attempt(
                    attempt_id, opportunity.buy, opportunity.sell, results, is_shadow
                )
            reservation = reserved
        # Concurrently: sending one and waiting would give the second leg a
        # head start the real system would not have.
        raw = await asyncio.gather(
            *(self._adapter.submit(request) for request in requests), return_exceptions=True
        )
        results = [
            value if isinstance(value, ExecutionResult) else self._failed(request, value)
            for request, value in zip(requests, raw, strict=True)
        ]
        attempt = self._attempt(attempt_id, opportunity.buy, opportunity.sell, results, is_shadow)
        if reservation is not None:
            assert self._account is not None
            if is_shadow:
                # A probe asks whether this one attempt was feasible from the
                # current baseline. It must not consume the real paper
                # portfolio or poison later strategy decisions.
                await self._account.release(reservation)
            else:
                await self._account.settle(reservation, attempt)
        _log(attempt, signal)
        return attempt

    def _attempt(
        self,
        attempt_id: str,
        buy: Leg,
        sell: Leg,
        results: list[ExecutionResult],
        is_shadow: bool,
    ) -> ExecutionAttempt:
        timestamps = [result.book_local_timestamp for result in results]
        skew = (
            int(abs((timestamps[0] - timestamps[1]).total_seconds()) * 1000)
            if timestamps[0] is not None and timestamps[1] is not None
            else None
        )
        return ExecutionAttempt(
            attempt_id=attempt_id,
            buy=LegOutcome(leg=buy, result=results[0]),
            sell=LegOutcome(leg=sell, result=results[1]),
            is_shadow=is_shadow,
            timing_skew_ms=skew,
            timing_violation=skew is not None and skew > self._max_leg_skew_ms,
        )

    def _request(
        self, leg: Leg, intent_id: str, attempt_id: str, index: int, signal: Signal
    ) -> OrderRequest:
        """One leg as an order. The limit price is the strategy's own.

        A limit order at the price the strategy expected is IOC. It can cap
        adverse price, but it cannot earn maker status merely by being called
        a limit order.
        """
        limit = self._limit_price(leg) if self._order_type is OrderType.LIMIT else None
        return OrderRequest(
            ref=leg.ref,
            side=leg.side,
            quantity=leg.quantity,
            order_type=self._order_type,
            intent=OrderIntent.OPEN,
            price=limit,
            time_in_force=TimeInForce.IOC if self._order_type is OrderType.LIMIT else None,
            expected_price=leg.executable_price,
            # Deterministic per attempt and leg, so a retry of the same
            # attempt reuses it and cannot open a second position.
            client_order_id=f"{attempt_id}-{index}",
            execution_intent_id=intent_id,
            attempt_id=attempt_id,
            strategy=signal.strategy,
            signal_generated_at=signal.generated_at,
            signal_expires_at=signal.expires_at,
            expected_net_edge_bps=signal.expected_net_edge_bps,
            signal_leg=index,
        )

    def _expired(self, request: OrderRequest) -> ExecutionResult:
        now = self._clock()
        return ExecutionResult(
            request=request,
            status=OrderStatus.EXPIRED,
            fills=(),
            submitted_at=now,
            acknowledged_at=None,
            closed_at=now,
            latency_ms=0,
            terminal_latency_ms=0,
            rejection=RejectionCode.SIGNAL_EXPIRED,
            detail="signal expired before the execution worker submitted it",
        )

    def _failed(self, request: OrderRequest, error: BaseException) -> ExecutionResult:
        now = self._clock()
        logger.error(
            "execution.leg_failed", client_order_id=request.client_order_id, error=repr(error)
        )
        return ExecutionResult(
            request=request,
            status=OrderStatus.FAILED,
            fills=(),
            submitted_at=now,
            acknowledged_at=None,
            closed_at=now,
            latency_ms=0,
            terminal_latency_ms=0,
            rejection=RejectionCode.ADAPTER_ERROR,
            detail=f"{type(error).__name__}: {error}",
        )

    def _refused(self, request: OrderRequest, rejection: AccountRejection) -> ExecutionResult:
        now = self._clock()
        return ExecutionResult(
            request=request,
            status=OrderStatus.REJECTED,
            fills=(),
            submitted_at=now,
            acknowledged_at=None,
            closed_at=now,
            latency_ms=0,
            terminal_latency_ms=0,
            rejection=rejection.code,
            detail=rejection.detail,
        )

    def _limit_price(self, leg: Leg) -> Decimal:
        """The strategy's price, rounded to a tick the venue will accept.

        Rounded *against* us - down for a buy, up for a sell - so the
        adjustment never improves on what the book actually showed. Rounding
        the other way would place an order at a price nobody offered.
        """
        price = leg.executable_price
        spec = self._specs(leg.ref) if self._specs is not None else None
        tick = spec.tick_size if spec is not None else None
        if tick is None or tick <= 0:
            return price
        steps = price / tick
        rounded = (
            steps.to_integral_value(rounding=ROUND_FLOOR)
            if leg.side is Side.BUY
            else steps.to_integral_value(rounding=ROUND_CEILING)
        ) * tick
        return rounded if rounded > 0 else price


def _log(attempt: ExecutionAttempt, signal: Signal) -> None:
    unfilled = [
        outcome for outcome in attempt.legs if outcome.result.status is not OrderStatus.FILLED
    ]
    logger.info(
        "execution.attempt",
        strategy=signal.strategy,
        attempt=attempt.attempt_id,
        shadow=attempt.is_shadow,
        outcome=attempt.describe(),
        expected_net_bps=str(signal.expected_net_edge_bps.quantize(Decimal("0.01"))),
        realised_slippage_bps=_slippage_summary(attempt.legs),
        failures=[
            f"{o.leg.ref.symbol}:{o.result.status.value}"
            f"{'/' + o.result.rejection.value if o.result.rejection else ''}"
            for o in unfilled
        ],
        timing_skew_ms=attempt.timing_skew_ms,
        timing_violation=attempt.timing_violation,
    )


def _attempt_id(intent_id: str) -> str:
    """A bounded deterministic id safe for exchange client-id limits."""
    return hashlib.sha256(intent_id.encode("utf-8")).hexdigest()[:24]


def _slippage_summary(legs: Sequence[LegOutcome]) -> str | None:
    """Both legs' realised slippage against what the strategy expected.

    The one number that says whether the cost model was telling the truth,
    and the first time the system has been able to produce it.
    """
    measured = [
        outcome.result.slippage_bps for outcome in legs if outcome.result.slippage_bps is not None
    ]
    if not measured:
        return None
    total = sum(measured, Decimal(0))
    return f"{total.quantize(Decimal('0.01'))}"
