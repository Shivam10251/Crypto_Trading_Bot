"""Writing simulated orders and fills to the execution record.

Every attempt is stored, including the ones that filled nothing. An execution
dataset containing only successful fills cannot answer the questions Phase 8
exists to answer - how often does a leg miss, how often does depth run out,
how often does a resting order expire unfilled - and those are the numbers
that decide whether the strategy is executable at all.

Orders and fills are written together, in one transaction, because a fill
without its order is not interpretable and an order whose fills went missing
reads as a rejection that never happened.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.core.logging import get_logger
from trading_bot.db.models import Fill as FillRow
from trading_bot.db.models import Order as OrderRow
from trading_bot.db.models import Position as PositionRow
from trading_bot.db.models.enums import ExecutionMode, OrderStatus, PositionStatus
from trading_bot.exchange.models import MarketRef
from trading_bot.execution.coordinator import ExecutionAttempt, LegOutcome
from trading_bot.execution.models import ExecutionResult

logger = get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# Attempts held while the database is unreachable, matching the opportunity
# recorder's policy: drop the oldest with a warning rather than grow forever.
MAX_PENDING_ATTEMPTS = 2000


def order_row(
    outcome: LegOutcome,
    market_ids: dict[MarketRef, int],
    *,
    opportunity_uid: uuid.UUID | None,
    is_shadow: bool,
) -> dict[str, Any] | None:
    """One leg as an ``orders`` row, or ``None`` if its market is unknown."""
    result = outcome.result
    request = result.request
    market_id = market_ids.get(request.ref)
    if market_id is None:
        return None
    return {
        "market_id": market_id,
        "mode": result.mode,
        "opportunity_uid": opportunity_uid,
        "is_shadow": is_shadow,
        "risk_event_id": request.risk_event_id,
        "execution_intent_id": request.execution_intent_id,
        "attempt_id": request.attempt_id,
        "signal_leg": request.signal_leg,
        "intent": request.intent.value,
        "strategy": request.strategy,
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
        # Never leave a non-fill unexplained.
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
            "attempt_id": request.attempt_id,
            "execution_intent_id": request.execution_intent_id,
            "order_intent": request.intent.value,
            "expected_price": str(request.expected_price) if request.expected_price else None,
            "time_in_force": request.time_in_force.value if request.time_in_force else None,
            "signal_generated_at": (
                request.signal_generated_at.isoformat() if request.signal_generated_at else None
            ),
            "signal_expires_at": (
                request.signal_expires_at.isoformat() if request.signal_expires_at else None
            ),
            "expected_net_edge_bps": (
                str(request.expected_net_edge_bps)
                if request.expected_net_edge_bps is not None
                else None
            ),
            "rejection_code": result.rejection.value if result.rejection else None,
            "detail": result.detail,
            "venue_filters": dict(result.venue_filters) if result.venue_filters else None,
        },
    }


def _reason(result: ExecutionResult) -> str | None:
    if result.rejection is None:
        return None
    if result.detail:
        return f"{result.rejection.value}: {result.detail}"
    return result.rejection.value


def fill_rows(
    outcome: LegOutcome,
    order_id: int,
    mode: ExecutionMode,
    *,
    position_id: int | None = None,
) -> list[dict[str, Any]]:
    """One row per simulated execution; a partial fill produces fewer, not none."""
    return [
        {
            "order_id": order_id,
            "mode": mode,
            "position_id": position_id,
            # NULL: no venue produced this, and inventing an id would make a
            # simulated fill indistinguishable from a real one.
            "exchange_fill_id": None,
            "price": fill.price,
            "quantity": fill.quantity,
            "fee_usd": fill.fee_usd,
            "fee_asset": fill.fee_asset,
            "fee_rate_bps": fill.fee_rate_bps,
            "slippage_bps": fill.slippage_bps,
            "is_maker": fill.is_maker,
            "filled_at": fill.filled_at,
            "latency_ms": outcome.result.latency_ms,
            "fill_index": index,
            "book_sequence": fill.book_sequence or outcome.result.book_sequence,
            "book_local_timestamp": (
                fill.book_local_timestamp or outcome.result.book_local_timestamp
            ),
            "levels": [
                {"price": str(level.price), "quantity": str(level.size)} for level in fill.levels
            ],
        }
        for index, fill in enumerate(outcome.result.fills)
    ]


class ExecutionRecorder:
    """Persists execution attempts: both legs' orders and all their fills."""

    def __init__(
        self,
        market_ids: dict[MarketRef, int],
        session_factory: SessionFactory,
        *,
        interval_seconds: float,
    ) -> None:
        self._market_ids = market_ids
        self._session_factory = session_factory
        self._interval = interval_seconds
        self._pending: list[tuple[ExecutionAttempt, uuid.UUID | None]] = []
        self.orders_written = 0
        self.fills_written = 0
        self.unhedged = 0
        self.failures = 0

    def record(self, attempt: ExecutionAttempt, opportunity_uid: uuid.UUID | None = None) -> None:
        """Queue one attempt; written with the next flush."""
        if len(self._pending) >= MAX_PENDING_ATTEMPTS:
            logger.warning("execution.attempt_dropped", attempt=attempt.attempt_id)
            self._pending.pop(0)
        self._pending.append((attempt, opportunity_uid))
        if not attempt.is_hedged and not attempt.is_empty:
            self.unhedged += 1

    async def flush(self) -> int:
        """Write queued attempts; returns order rows written.

        A database failure keeps the queue and retries. The retry is safe
        because ``client_order_id`` is derived from the attempt id, so the
        unique index refuses a second copy of an order that did land.
        """
        if not self._pending:
            return 0
        # Swap before the first await.  Producers can append to a fresh queue
        # while this batch is in flight without being removed by a later slice.
        batch, self._pending = self._pending, []
        rows: list[tuple[LegOutcome, dict[str, Any]]] = []
        for attempt, opportunity_uid in batch:
            for outcome in attempt.legs:
                row = order_row(
                    outcome,
                    self._market_ids,
                    opportunity_uid=opportunity_uid,
                    is_shadow=attempt.is_shadow,
                )
                if row is None:
                    logger.warning("execution.market_not_registered", market=str(outcome.leg.ref))
                    continue
                rows.append((outcome, row))
        if not rows:
            return 0

        try:
            async with self._session_factory() as session:
                written = await self._write(session, rows)
        except Exception as exc:
            self._pending = (batch + self._pending)[-MAX_PENDING_ATTEMPTS:]
            self.failures += 1
            logger.warning("execution.write_failed", error=str(exc), pending=len(batch))
            return 0

        self.orders_written += len(rows)
        self.fills_written += written
        return len(rows)

    async def _write(
        self, session: AsyncSession, rows: Sequence[tuple[LegOutcome, dict[str, Any]]]
    ) -> int:
        """Insert the orders, then the fills that reference them."""
        # sort_by_parameter_order for the same reason the opportunity recorder
        # needs it: without it PostgreSQL may return the generated ids in any
        # order and every fill would attach to the wrong order, silently.
        values = [row for _, row in rows]
        statement = insert(OrderRow).values(values)
        # A commit acknowledgement can be lost after PostgreSQL committed.
        # Retrying must converge on the existing rows, not fail forever on the
        # unique client id and leave the batch permanently stuck.
        updatable = {
            key: getattr(statement.excluded, key)
            for key in values[0]
            if key not in {"mode", "client_order_id"}
        }
        await session.execute(
            statement.on_conflict_do_update(
                constraint="mode_client_order_id",
                set_=updatable,
            )
        )
        client_ids = [row["client_order_id"] for row in values]
        result = await session.execute(
            select(OrderRow.id, OrderRow.client_order_id).where(
                OrderRow.mode == values[0]["mode"],
                OrderRow.client_order_id.in_(client_ids),
            )
        )
        ids = {client_id: row_id for row_id, client_id in result}
        position_values: list[dict[str, Any]] = []
        for outcome, row in rows:
            result_row = outcome.result
            request = result_row.request
            if (
                result_row.filled_quantity <= 0
                or result_row.average_price is None
                or request.attempt_id is None
            ):
                continue
            if request.expected_price is None:
                slippage_usd = Decimal(0)
            else:
                difference = (
                    result_row.average_price - request.expected_price
                    if request.side.value == "BUY"
                    else request.expected_price - result_row.average_price
                )
                # Signed, like fill.slippage_bps: negative means price
                # improvement and must not be rewritten as a cost.
                slippage_usd = difference * result_row.filled_quantity
            position_values.append(
                {
                    "market_id": row["market_id"],
                    "opportunity_uid": row["opportunity_uid"],
                    "attempt_id": request.attempt_id,
                    "is_shadow": row["is_shadow"],
                    "mode": row["mode"],
                    "strategy": request.strategy or "unknown",
                    "side": request.side,
                    "status": PositionStatus.OPEN,
                    "quantity": result_row.filled_quantity,
                    "entry_price": result_row.average_price,
                    "entry_notional_usd": result_row.notional,
                    "realized_pnl_usd": Decimal(0),
                    "unrealized_pnl_usd": Decimal(0),
                    "fees_usd": result_row.fees_usd,
                    "slippage_usd": slippage_usd,
                    "opened_at": result_row.closed_at or result_row.submitted_at,
                }
            )
        position_ids: dict[tuple[str, int], int] = {}
        if position_values:
            position_statement = insert(PositionRow).values(position_values)
            await session.execute(
                position_statement.on_conflict_do_update(
                    constraint="mode_attempt_market",
                    set_={
                        key: getattr(position_statement.excluded, key)
                        for key in position_values[0]
                        if key not in {"mode", "attempt_id", "market_id"}
                    },
                )
            )
            stored_positions = await session.execute(
                select(PositionRow.id, PositionRow.attempt_id, PositionRow.market_id).where(
                    PositionRow.mode == position_values[0]["mode"],
                    PositionRow.attempt_id.in_([value["attempt_id"] for value in position_values]),
                )
            )
            position_ids = {
                (attempt_id, market_id): position_id
                for position_id, attempt_id, market_id in stored_positions
                if attempt_id is not None
            }
        fills: list[dict[str, Any]] = []
        for outcome, row in rows:
            request = outcome.result.request
            position_id = (
                position_ids.get((request.attempt_id, row["market_id"]))
                if request.attempt_id is not None
                else None
            )
            fills.extend(
                fill_rows(
                    outcome,
                    ids[row["client_order_id"]],
                    row["mode"],
                    position_id=position_id,
                )
            )
        if fills:
            await session.execute(
                insert(FillRow)
                .values(fills)
                .on_conflict_do_update(
                    constraint="order_fill_index",
                    set_={
                        key: getattr(insert(FillRow).excluded, key)
                        for key in fills[0]
                        if key not in {"order_id", "fill_index"}
                    },
                )
            )
        return len(fills)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.flush()


def summarise(attempts: Sequence[ExecutionAttempt]) -> str:
    """One line of what execution actually did, for the terminal and the log."""
    if not attempts:
        return "no execution attempts"
    hedged = sum(a.is_hedged for a in attempts)
    empty = sum(a.is_empty for a in attempts)
    unhedged = len(attempts) - hedged - empty
    shadow = sum(a.is_shadow for a in attempts)
    parts = [f"{len(attempts)} attempts", f"{hedged} hedged"]
    if unhedged:
        parts.append(f"{unhedged} UNHEDGED")
    if empty:
        parts.append(f"{empty} filled nothing")
    if shadow:
        parts.append(f"{shadow} shadow probes")
    return ", ".join(parts)


def partial_or_worse(attempt: ExecutionAttempt) -> tuple[OrderStatus, ...]:
    """Each leg's status - what a run's honesty check reads."""
    return tuple(outcome.result.status for outcome in attempt.legs)
