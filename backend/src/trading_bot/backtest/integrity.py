"""A replay's result is published only if every record of it is durable.

The live service is built to survive: a failed write is queued and retried,
an exit sweep that cannot evaluate one attempt moves on, a risk decision that
cannot be stored fails closed, a feed that raises becomes "no book". Every one
of those is right for a process that must keep running - and every one is a
silent divergence for a replay, whose run would then describe a history no
real run could have had, next to rows that do not add up to it.

So replay keeps the live components and their behaviour, and checks what they
report instead:

- **Retryable**: a queued write that failed (orders, fills, positions,
  opportunities, signals, risk events). Flushed again immediately, up to
  ``backtest.persistence_attempts`` times. Every write is an idempotent
  upsert, so a retry after a lost acknowledgement converges on the rows that
  did land.
- **Fatal at once**: anything already lost or already decided differently -
  a record pushed out of a full queue, a row for an unregistered market, a
  risk decision that could not be stored (the engine refused the trade for
  it), a kill-switch read or listener failure, an unreadable P&L view, an exit
  sweep failure, a replay read refused for lack of lookahead.

**The rule.** Either failure ends the run ``FAILED`` with the reason; a
``FAILED`` run never carries a fingerprint or a completeness verdict that
would let its partial rows be read as a result. ``INCOMPLETE`` is reserved for
what the *data* or the *accounting* could not support, never for what the
replay failed to write. At the end, ``reconcile`` checks the in-memory paper
account against the durable fills and positions it was built from.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, select

from trading_bot.backtest.loop import SessionFactory
from trading_bot.core.logging import get_logger
from trading_bot.db.models import Fill, Market, Order, Position
from trading_bot.db.models.enums import LIVE_POSITION_STATUSES, MarketType
from trading_bot.db.scope import RunScope
from trading_bot.execution.account import PaperAccount

logger = get_logger(__name__)

#: Per-fill rounding can accumulate; remain strict, but never fail a large run
#: merely because its NUMERIC(28, 8) fee rows each lost a fraction of a cent.
_MIN_RECONCILE_TOLERANCE_USD = Decimal("0.0001")
_MAX_RECONCILE_TOLERANCE_USD = Decimal("0.01")
_MONEY_QUANTUM_USD = Decimal("0.00000001")


class ReplayIntegrityError(RuntimeError):
    """A record of the run was lost, or the run diverged from its records."""


class _Queued(Protocol):
    @property
    def pending(self) -> int: ...

    last_error: str | None

    def flush(self) -> Awaitable[int]: ...


@dataclass(frozen=True, slots=True)
class Probe:
    """One counter that must never move during a replay."""

    name: str
    read: Callable[[], int]
    detail: Callable[[], str | None] = lambda: None


class ReplayIntegrity:
    def __init__(self, *, probes: list[Probe], queues: dict[str, _Queued], attempts: int) -> None:
        self._probes = probes
        self._baseline = {probe.name: probe.read() for probe in probes}
        self._queues = queues
        self._attempts = attempts

    def check(self) -> None:
        for probe in self._probes:
            if probe.read() != self._baseline[probe.name]:
                detail = probe.detail()
                raise ReplayIntegrityError(
                    f"{probe.name} changed from {self._baseline[probe.name]} to {probe.read()}"
                    + (f": {detail}" if detail else "")
                )

    async def flush(self) -> None:
        """Write every queued record, or fail the run saying which could not be."""
        for name, queue in self._queues.items():
            for _ in range(self._attempts):
                await queue.flush()
                if queue.pending == 0:
                    break
            if queue.pending:
                raise ReplayIntegrityError(
                    f"{name}: {queue.pending} record(s) still unwritten after "
                    f"{self._attempts} flush attempt(s): {queue.last_error}"
                )
        self.check()


async def flush_best_effort(*queues: _Queued) -> None:
    """Write what can be written, for diagnosis; never raises."""
    for queue in queues:
        try:
            await queue.flush()
        except Exception:
            logger.exception("backtest.diagnostic_flush_failed")


async def durable_counts(session_factory: SessionFactory, scope: RunScope) -> tuple[int, int]:
    """Orders and fills the run durably holds, however they were written."""
    async with session_factory() as session:
        orders = await session.scalar(
            select(func.count(Order.id)).where(*scope.filters(Order.mode, Order.backtest_run_id))
        )
        fills = await session.scalar(
            select(func.count(Fill.id)).where(*scope.filters(Fill.mode, Fill.backtest_run_id))
        )
    return int(orders or 0), int(fills or 0)


async def reconcile(
    account: PaperAccount,
    *,
    session_factory: SessionFactory,
    scope: RunScope,
    durable_balances: Callable[[], Awaitable[tuple[Decimal, Decimal]]],
    bnb_price_usd: Decimal | None,
    initial_bnb: Decimal,
) -> None:
    """The in-memory ledger must equal what the durable rows say it is."""
    cash, bnb_fees_usd = await durable_balances()
    async with session_factory() as session:
        fill_count = int(
            await session.scalar(
                select(func.count(Fill.id)).where(*scope.filters(Fill.mode, Fill.backtest_run_id))
            )
            or 0
        )
        open_quantity = Position.quantity - Position.closed_quantity
        rows = await session.execute(
            select(
                Market.symbol,
                Market.market_type,
                func.sum(Position.entry_notional_usd * open_quantity / Position.quantity),
            )
            .join(Market, Market.id == Position.market_id)
            .where(
                *scope.filters(Position.mode, Position.backtest_run_id),
                Position.is_shadow.is_(False),
                Position.status.in_(LIVE_POSITION_STATUSES),
                open_quantity > 0,
            )
            .group_by(Market.symbol, Market.market_type)
        )
        durable_gross: dict[tuple[str, MarketType], Decimal] = {
            (symbol, market_type): value for symbol, market_type, value in rows
        }
    problems: list[str] = []
    tolerance = min(
        _MAX_RECONCILE_TOLERANCE_USD,
        max(_MIN_RECONCILE_TOLERANCE_USD, fill_count * _MONEY_QUANTUM_USD * 2),
    )
    if abs(cash - account.cash_usd) > tolerance:
        problems.append(f"cash: account {account.cash_usd} vs durable {cash}")
    if bnb_price_usd is not None:
        spent_usd = (initial_bnb - account.bnb_balance) * bnb_price_usd
        if abs(spent_usd - bnb_fees_usd) > tolerance:
            problems.append(f"BNB fees: account {spent_usd} vs durable {bnb_fees_usd}")
    ledger = account.position_gross_usd
    for key in sorted(set(ledger) | set(durable_gross), key=str):
        held, stored = ledger.get(key, Decimal(0)), durable_gross.get(key, Decimal(0))
        if abs(held - stored) > tolerance:
            problems.append(f"{key[0]}/{key[1].value} exposure: account {held} vs durable {stored}")
    if problems:
        raise ReplayIntegrityError(
            "the paper account diverged from the run's durable records: " + "; ".join(problems)
        )
