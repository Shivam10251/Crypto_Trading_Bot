"""Turning one close leg into the rows that prove it happened.

Separated from the orchestration so ``closer`` reads as the sequence of
decisions it is - claim, ask risk, submit both legs, record, settle - rather
than as decisions interleaved with column mapping.

Every row built here is an upsert target. The close's identity is
deterministic (``close:<attempt>:<sequence>``), so a replay after a lost
acknowledgement produces the same ``client_order_id`` and the same
``fill_index``, and both unique constraints turn the second write into an
update rather than a duplicate.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_bot.core.logging import get_logger
from trading_bot.db.models.enums import ExecutionMode, OrderStatus, OrderType
from trading_bot.execution.models import (
    ExecutionResult,
    OrderIntent,
    OrderRequest,
    RejectionCode,
)
from trading_bot.portfolio.records import AttemptRecord, LegRecord, close_client_order_id
from trading_bot.portfolio.valuation import exit_side

logger = get_logger(__name__)


def close_request(
    leg: LegRecord,
    intent_id: str,
    index: int,
    strategy: str,
    risk_event_id: int | None,
) -> OrderRequest:
    """One leg's close.

    Always a market order. An IOC limit can leave part of one leg unfilled
    while the other completes - Phase 8 measured that on 3 of 21 limit
    entries - and on an *exit* that failure mode creates the naked exposure
    the close was called to remove.
    """
    return OrderRequest(
        ref=leg.ref,
        side=exit_side(leg.side),
        quantity=leg.open_quantity,
        order_type=OrderType.MARKET,
        intent=OrderIntent.CLOSE,
        # Deterministic within this claim, so replaying its record updates the
        # same order. A later terminal retry gets a new claim sequence.
        client_order_id=close_client_order_id(intent_id, index),
        execution_intent_id=intent_id,
        attempt_id=leg.attempt_id,
        strategy=strategy,
        signal_leg=index,
        risk_event_id=risk_event_id,
    )


def order_values(
    leg: LegRecord,
    request: OrderRequest,
    result: ExecutionResult,
    attempt: AttemptRecord,
    intent_id: str,
    index: int,
    backtest_run_id: int | None = None,
) -> dict[str, Any]:
    return {
        "market_id": leg.market_id,
        "mode": result.mode,
        "backtest_run_id": backtest_run_id,
        "opportunity_uid": None,
        "is_shadow": False,
        "risk_event_id": request.risk_event_id,
        "execution_intent_id": intent_id,
        "attempt_id": attempt.attempt_id,
        "signal_leg": index,
        "intent": OrderIntent.CLOSE.value,
        "strategy": attempt.strategy,
        "client_order_id": request.client_order_id,
        "exchange_order_id": result.exchange_order_id,
        "side": request.side,
        "order_type": request.order_type,
        "time_in_force": request.time_in_force,
        "quantity": request.quantity,
        "price": request.price,
        "expected_price": request.expected_price,
        "filled_quantity": result.filled_quantity,
        "average_fill_price": result.average_price,
        "status": result.status,
        "rejection_reason": _reason(result),
        "submitted_at": result.submitted_at,
        "acknowledged_at": result.acknowledged_at,
        "closed_at": result.closed_at,
        "latency_ms": result.latency_ms,
        "terminal_latency_ms": result.terminal_latency_ms,
        "book_sequence": result.book_sequence,
        "book_local_timestamp": result.book_local_timestamp,
        "evidence": {
            "schema": 1,
            "order_intent": OrderIntent.CLOSE.value,
            "closes_position_id": leg.position_id,
            "entry_side": leg.side.value,
            "entry_price": str(leg.entry_price),
            "open_quantity_before": str(leg.open_quantity),
            "rejection_code": result.rejection.value if result.rejection else None,
            "detail": result.detail,
            "venue_filters": dict(result.venue_filters) if result.venue_filters else None,
        },
    }


def fill_values(
    leg: LegRecord,
    result: ExecutionResult,
    order_id: int,
    mode: ExecutionMode,
    backtest_run_id: int | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "order_id": order_id,
            "mode": mode,
            "backtest_run_id": backtest_run_id,
            # Linked at write time, which is what lets the position's exit
            # accounting be recomputed from fills alone.
            "position_id": leg.position_id,
            "exchange_fill_id": None,
            "price": fill.price,
            "quantity": fill.quantity,
            "fee_usd": fill.fee_usd,
            "fee_asset": fill.fee_asset,
            "fee_rate_bps": fill.fee_rate_bps,
            "slippage_bps": fill.slippage_bps,
            "is_maker": fill.is_maker,
            "filled_at": fill.filled_at,
            "latency_ms": result.latency_ms,
            "fill_index": index,
            "book_sequence": fill.book_sequence or result.book_sequence,
            "book_local_timestamp": fill.book_local_timestamp or result.book_local_timestamp,
            "levels": [
                {"price": str(level.price), "quantity": str(level.size)} for level in fill.levels
            ],
        }
        for index, fill in enumerate(result.fills)
    ]


def _reason(result: ExecutionResult) -> str | None:
    if result.rejection is None:
        return None
    return f"{result.rejection.value}: {result.detail}" if result.detail else result.rejection.value


def failed_result(request: OrderRequest, error: BaseException, now: datetime) -> ExecutionResult:
    logger.error(
        "portfolio.close_leg_failed", client_order_id=request.client_order_id, error=repr(error)
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


def left_unhedged(legs: Sequence[LegRecord], results: Sequence[ExecutionResult]) -> bool:
    """True when this close leaves any non-zero, unbalanced exposure.

    Real residual risk, reported rather than smoothed over: the pair no longer
    hedges itself, and the next sweep will see it as ``UNPAIRED_RESIDUAL``.
    """
    remaining = [
        leg.open_quantity - result.filled_quantity
        for leg, result in zip(legs, results, strict=True)
    ]
    positive = [max(Decimal(0), value) for value in remaining]
    if not any(positive):
        return False
    return len(positive) != 2 or positive[0] != positive[1]
