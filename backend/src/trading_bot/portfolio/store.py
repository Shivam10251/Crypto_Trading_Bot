"""Reading exposure and writing what closing it did, transactionally.

Two invariants shape every query here.

**Exit accounting is recomputed, never incremented.** ``reconcile`` reads a
position's whole set of **durable** fills and writes the result. That makes
repeated reconciliation and a restart after fills were committed converge on
the same row: applying a delta twice double-counts, recomputing twice does
not. A paper fill lost before commit cannot be recovered from the database.
Recomputation is not a later cost
model rewriting history either - the numbers come from the fills' own stored
fees and prices - and a position already ``CLOSED`` is never revisited, which
is where that guarantee is enforced rather than promised.

**A position is claimed before it is closed.** ``claim`` row-locks the whole
attempt, rejects a stale caller, and flips its live legs to ``CLOSING`` in one
transaction, so two workers cannot both close the same position. The claim
also fixes one close-attempt identity; a later terminal retry gets the next
sequence rather than overwriting that attempt.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import Row, Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.core.logging import get_logger
from trading_bot.db.models import Fill as FillRow
from trading_bot.db.models import Market as MarketRow
from trading_bot.db.models import Order as OrderRow
from trading_bot.db.models import Position as PositionRow
from trading_bot.db.models.enums import (
    LIVE_POSITION_STATUSES,
    ExecutionMode,
    MarketType,
    PositionStatus,
    Side,
)
from trading_bot.exchange.models import MarketRef
from trading_bot.execution.models import OrderIntent
from trading_bot.portfolio.accounting import FillLot, PositionPnl, position_pnl
from trading_bot.portfolio.records import (
    AttemptRecord,
    CloseClaim,
    LegRecord,
    close_intent_id,
)

logger = get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class PortfolioStore:
    """Every durable read and write the portfolio subsystem makes."""

    def __init__(
        self, session_factory: SessionFactory, *, mode: ExecutionMode = ExecutionMode.PAPER
    ) -> None:
        self._session_factory = session_factory
        self._mode = mode

    @property
    def mode(self) -> ExecutionMode:
        return self._mode

    # --- reading --------------------------------------------------------

    def _positions(self) -> Select[tuple[PositionRow, str, MarketType]]:
        return (
            select(PositionRow, MarketRow.symbol, MarketRow.market_type)
            .join(MarketRow, MarketRow.id == PositionRow.market_id)
            .where(
                PositionRow.mode == self._mode,
                # A probe's exposure is hypothetical. It is never valued,
                # never closed and never counted in a portfolio total.
                PositionRow.is_shadow.is_(False),
                PositionRow.attempt_id.is_not(None),
            )
        )

    async def live_attempts(self, venue: str) -> list[AttemptRecord]:
        """Every attempt with exposure left, with its fills attached."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    self._positions().where(PositionRow.status.in_(LIVE_POSITION_STATUSES))
                )
            ).all()
            attempt_ids = {row[0].attempt_id for row in rows}
            if not attempt_ids:
                return []
            # Both legs of any attempt with a live leg, so a pair whose other
            # leg already closed is visible as the residual exposure it is.
            everything = (
                await session.execute(
                    self._positions().where(PositionRow.attempt_id.in_(attempt_ids))
                )
            ).all()
            lots = await self._fill_lots(session, [row[0].id for row in everything])
        return _group(everything, lots, venue)

    async def attempt(self, attempt_id: str, venue: str) -> AttemptRecord | None:
        """Reload one attempt after recovery or another state transition."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(self._positions().where(PositionRow.attempt_id == attempt_id))
            ).all()
            if not rows:
                return None
            lots = await self._fill_lots(session, [row[0].id for row in rows])
        grouped = _group(rows, lots, venue)
        return grouped[0] if grouped else None

    async def attempts_closed_between(
        self, venue: str, start: datetime | None, end: datetime
    ) -> list[AttemptRecord]:
        """Attempts whose **last** leg closed in ``[start, end)``.

        The window is applied to the attempt, not to its legs: a basis trade
        completes when the pair is flat, and attributing one leg to yesterday
        and the other to today would report two half-trades that never
        happened.
        """
        conditions = [PositionRow.closed_at < end]
        if start is not None:
            conditions.append(PositionRow.closed_at >= start)
        async with self._session_factory() as session:
            candidates = (
                await session.execute(
                    select(PositionRow.attempt_id)
                    .where(
                        PositionRow.mode == self._mode,
                        PositionRow.is_shadow.is_(False),
                        PositionRow.attempt_id.is_not(None),
                        PositionRow.status == PositionStatus.CLOSED,
                        *conditions,
                    )
                    .distinct()
                )
            ).scalars()
            attempt_ids = {value for value in candidates if value is not None}
            if not attempt_ids:
                return []
            rows = (
                await session.execute(
                    self._positions().where(PositionRow.attempt_id.in_(attempt_ids))
                )
            ).all()
            lots = await self._fill_lots(session, [row[0].id for row in rows])
        attempts = _group(rows, lots, venue)
        return [
            attempt
            for attempt in attempts
            if _completed_within(attempt, start, end) and attempt.paired().is_complete
        ]

    async def latest_closed_attempts(
        self, venue: str, end: datetime, *, limit: int
    ) -> list[AttemptRecord]:
        """Newest complete two-leg attempts, bounded for loss-streak reads."""
        if limit <= 0:
            return []
        async with self._session_factory() as session:
            candidates = (
                await session.execute(
                    select(PositionRow.attempt_id)
                    .where(
                        PositionRow.mode == self._mode,
                        PositionRow.is_shadow.is_(False),
                        PositionRow.attempt_id.is_not(None),
                        PositionRow.closed_at < end,
                    )
                    .group_by(PositionRow.attempt_id)
                    .having(
                        func.count(PositionRow.id) == 2,
                        func.count(PositionRow.id).filter(
                            PositionRow.status == PositionStatus.CLOSED
                        )
                        == 2,
                    )
                    .order_by(func.max(PositionRow.closed_at).desc())
                    .limit(limit)
                )
            ).scalars()
            attempt_ids = [value for value in candidates if value is not None]
            if not attempt_ids:
                return []
            rows = (
                await session.execute(
                    self._positions().where(PositionRow.attempt_id.in_(attempt_ids))
                )
            ).all()
            lots = await self._fill_lots(session, [row[0].id for row in rows])
        attempts = _group(rows, lots, venue)
        attempts.sort(
            key=lambda attempt: (
                attempt.paired().closed_at or datetime.min.replace(tzinfo=end.tzinfo)
            ),
            reverse=True,
        )
        return attempts

    async def _fill_lots(
        self, session: AsyncSession, position_ids: Sequence[int]
    ) -> dict[tuple[int, str], list[FillLot]]:
        return await _fill_lots_for(session, self._mode, position_ids)

    # --- claiming -------------------------------------------------------

    async def claim(
        self,
        attempt: AttemptRecord,
        *,
        reason: str,
        now: datetime,
        claim_timeout: timedelta,
        claim_id: str | None = None,
    ) -> CloseClaim | None:
        """Take both live legs of ``attempt`` for closing. ``None`` if lost.

        Rows are locked and compared with the caller's view before mutation.
        This prevents a partial reconciliation between policy evaluation and
        submission from turning a reduce-only close into an over-close.
        """
        legs = attempt.live_legs
        if not legs:
            return None
        stale_before = now - claim_timeout
        expected = {leg.position_id: leg for leg in legs}
        actual_claim_id = claim_id or uuid.uuid4().hex[:32]
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    self._positions()
                    .where(PositionRow.attempt_id == attempt.attempt_id)
                    .with_for_update(of=PositionRow)
                )
            ).all()
            current = _group(rows, {}, legs[0].ref.venue)
            if len(current) != 1:
                raise _ClaimLost(attempt.attempt_id)
            fresh = current[0]
            fresh_live = {leg.position_id: leg for leg in fresh.live_legs}
            if fresh_live.keys() != expected.keys():
                raise _ClaimLost(attempt.attempt_id)
            for position_id, leg in fresh_live.items():
                old = expected[position_id]
                claimable = leg.status is PositionStatus.OPEN or (
                    leg.status is PositionStatus.CLOSING
                    and (leg.close_claimed_at is None or leg.close_claimed_at < stale_before)
                )
                if (
                    not claimable
                    or leg.quantity != old.quantity
                    or leg.closed_quantity != old.closed_quantity
                    or leg.close_attempts != old.close_attempts
                ):
                    raise _ClaimLost(attempt.attempt_id)

            sequence = fresh.close_attempts
            intent_id = close_intent_id(fresh.attempt_id, sequence)
            by_id = {row.id: row for row, _, _ in rows}
            for position_id in fresh_live:
                row = by_id[position_id]
                row.status = PositionStatus.CLOSING
                row.close_intent_id = intent_id
                row.close_claim_id = actual_claim_id
                row.close_claimed_at = now
                row.close_attempts = sequence + 1
                row.exit_reason = reason
            await session.flush()
            claimed_rows = _group(rows, {}, legs[0].ref.venue)[0]
        return CloseClaim(intent_id, actual_claim_id, claimed_rows)

    # --- writing --------------------------------------------------------

    async def reconcile(self, position_ids: Sequence[int], *, now: datetime) -> list[int]:
        """Recompute each position from its fills; return the ids now CLOSED.

        Idempotent by construction: the result depends only on the fills, so
        running it twice writes the same values. A position already ``CLOSED``
        is skipped, which is what stops a later run - or a later cost model -
        from rewriting a settled result.
        """
        if not position_ids:
            return []
        async with self._session_factory() as session:
            return await reconcile_within(session, self._mode, position_ids, now=now)

    async def release_claim(
        self, position_ids: Sequence[int], *, claim_id: str, now: datetime
    ) -> None:
        """Hand back a claim on positions that are still carrying exposure."""
        if not position_ids:
            return
        async with self._session_factory() as session:
            await session.execute(
                update(PositionRow)
                .where(
                    PositionRow.id.in_(position_ids),
                    PositionRow.mode == self._mode,
                    PositionRow.status == PositionStatus.CLOSING,
                    PositionRow.close_claim_id == claim_id,
                )
                .values(status=PositionStatus.OPEN, close_claim_id=None, close_claimed_at=None)
                .execution_options(synchronize_session=False)
            )

    async def record_marks(
        self, marks: dict[int, tuple[Decimal, Decimal]], *, now: datetime
    ) -> None:
        """Store the latest honest mark and unrealized P&L per position.

        Positions absent from ``marks`` keep their previous values rather than
        being reset: the row says when it was last marked, so a stale figure
        is visible as stale instead of being silently refreshed or zeroed.
        """
        if not marks:
            return
        async with self._session_factory() as session:
            for position_id, (mark, unrealized) in marks.items():
                await session.execute(
                    update(PositionRow)
                    .where(PositionRow.id == position_id, PositionRow.mode == self._mode)
                    .values(mark_price=mark, marked_at=now, unrealized_pnl_usd=unrealized)
                    .execution_options(synchronize_session=False)
                )


async def reconcile_within(
    session: AsyncSession,
    mode: ExecutionMode,
    position_ids: Sequence[int],
    *,
    now: datetime,
) -> list[int]:
    """Recompute positions from their fills **inside a caller's transaction**.

    Exposed as a free function because closing is one transaction: the close's
    orders, its fills and the position rows they settle have to commit
    together or not at all. A crash between them would otherwise leave fills
    with no position update - exposure the system believes it still has - or a
    closed position with no fills to prove it.
    """
    if not position_ids:
        return []
    closed: list[int] = []
    # Row-locked for the life of the transaction, so a concurrent reconcile of
    # the same position waits rather than racing it.
    rows = (
        await session.execute(
            select(PositionRow, MarketRow.market_type)
            .join(MarketRow, MarketRow.id == PositionRow.market_id)
            .where(
                PositionRow.id.in_(position_ids),
                PositionRow.mode == mode,
                PositionRow.status.in_(LIVE_POSITION_STATUSES),
            )
            .with_for_update(of=PositionRow)
        )
    ).all()
    lots = await _fill_lots_for(session, mode, [row.id for row, _ in rows])
    for row, market_type in rows:
        pnl = position_pnl(
            side=row.side,
            entries=tuple(lots.get((row.id, OrderIntent.OPEN.value), ())),
            exits=tuple(lots.get((row.id, OrderIntent.CLOSE.value), ())),
            funding_pnl_usd=(Decimal(0) if market_type is MarketType.SPOT else None),
            borrow_cost_usd=None,
            borrows=market_type is MarketType.SPOT and row.side is Side.SELL,
        )
        _apply(row, pnl, now=now)
        if row.status is PositionStatus.CLOSED:
            closed.append(row.id)
    return closed


async def _fill_lots_for(
    session: AsyncSession, mode: ExecutionMode, position_ids: Sequence[int]
) -> dict[tuple[int, str], list[FillLot]]:
    """Fills keyed by (position, order intent).

    Classified by the *order's* intent rather than by a flag on the fill: the
    order is what declared whether it was taking exposure on or giving it
    back, and a fill inherits that rather than restating it.
    """
    if not position_ids:
        return {}
    rows = await session.execute(
        select(
            FillRow.position_id,
            OrderRow.intent,
            FillRow.price,
            FillRow.quantity,
            FillRow.fee_usd,
            FillRow.filled_at,
            OrderRow.expected_price,
        )
        .join(OrderRow, OrderRow.id == FillRow.order_id)
        .where(FillRow.position_id.in_(position_ids), FillRow.mode == mode)
        .order_by(FillRow.filled_at, FillRow.id)
    )
    lots: dict[tuple[int, str], list[FillLot]] = {}
    for position_id, intent, price, quantity, fee, filled_at, expected in rows:
        if position_id is None:
            continue
        key = (int(position_id), intent or OrderIntent.OPEN.value)
        lots.setdefault(key, []).append(
            FillLot(
                price=price,
                quantity=quantity,
                fee_usd=fee,
                filled_at=filled_at,
                expected_price=expected,
            )
        )
    return lots


class _ClaimLost(RuntimeError):
    """Another worker holds part of this attempt; abandon the whole claim."""

    def __init__(self, attempt_id: str) -> None:
        super().__init__(f"close claim lost for attempt {attempt_id}")
        self.attempt_id = attempt_id


ClaimLost = _ClaimLost


def _apply(row: PositionRow, pnl: PositionPnl, *, now: datetime) -> None:
    """Write one recomputed position back onto its row."""
    row.closed_quantity = pnl.exit.quantity
    row.exit_price = pnl.exit.price
    row.exit_notional_usd = pnl.exit.notional if pnl.exit.quantity > 0 else None
    row.exit_fees_usd = pnl.exit.fees_usd
    row.exit_slippage_usd = pnl.exit.slippage_usd
    row.fees_usd = pnl.fees_usd
    row.slippage_usd = pnl.slippage_usd
    row.price_pnl_usd = pnl.price_pnl_usd if pnl.exit.quantity > 0 else None
    row.realized_pnl_usd = pnl.realized_pnl_usd if pnl.exit.quantity > 0 else Decimal(0)
    row.funding_pnl_usd = pnl.funding_pnl_usd
    row.borrow_cost_usd = pnl.borrow_cost_usd
    row.unmeasured_pnl = (list(pnl.unmeasured) or None) if pnl.exit.quantity > 0 else None
    if pnl.is_flat:
        row.status = PositionStatus.CLOSED
        row.closed_at = pnl.exit.last_at or now
        row.close_claim_id = None
        row.close_claimed_at = None
        row.unrealized_pnl_usd = Decimal(0)
    elif row.status is PositionStatus.CLOSED:  # pragma: no cover - guarded by the query
        raise RuntimeError(f"position {row.id} is CLOSED but not flat")


def _completed_within(attempt: AttemptRecord, start: datetime | None, end: datetime) -> bool:
    closures = [leg.closed_at for leg in attempt.legs]
    if not closures or any(at is None for at in closures):
        return False
    completed = max(at for at in closures if at is not None)
    return (start is None or completed >= start) and completed < end


def _group(
    rows: Sequence[Row[tuple[PositionRow, str, MarketType]]],
    lots: dict[tuple[int, str], list[FillLot]],
    venue: str,
) -> list[AttemptRecord]:
    by_attempt: dict[str, list[LegRecord]] = {}
    strategies: dict[str, str] = {}
    mode: ExecutionMode | None = None
    for row, symbol, market_type in rows:
        if row.attempt_id is None:  # pragma: no cover - filtered by the query
            continue
        mode = row.mode
        strategies.setdefault(row.attempt_id, row.strategy)
        by_attempt.setdefault(row.attempt_id, []).append(
            LegRecord(
                position_id=row.id,
                market_id=row.market_id,
                ref=MarketRef(venue, symbol, market_type),
                market_type=market_type,
                mode=row.mode,
                strategy=row.strategy,
                attempt_id=row.attempt_id,
                side=row.side,
                status=row.status,
                quantity=row.quantity,
                closed_quantity=row.closed_quantity,
                entry_price=row.entry_price,
                opened_at=row.opened_at,
                closed_at=row.closed_at,
                close_intent_id=row.close_intent_id,
                close_claimed_at=row.close_claimed_at,
                close_attempts=row.close_attempts,
                entries=tuple(lots.get((row.id, OrderIntent.OPEN.value), ())),
                exits=tuple(lots.get((row.id, OrderIntent.CLOSE.value), ())),
            )
        )
    assert mode is not None or not by_attempt
    return [
        AttemptRecord(
            attempt_id=attempt_id,
            mode=legs[0].mode,
            strategy=strategies[attempt_id],
            legs=tuple(sorted(legs, key=lambda leg: leg.position_id)),
        )
        for attempt_id, legs in sorted(by_attempt.items())
    ]
