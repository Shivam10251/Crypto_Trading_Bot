"""Valuing the open book from the current books - no database, no writes.

Split from ``snapshots`` so the valuation rules read on their own: what an
open leg is worth, when a total may be published at all, and which carrying
costs a value omits. ``snapshots`` persists what this computes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_bot.db.models.enums import MarketType, Side, ValuationStatus
from trading_bot.db.scope import RunScope
from trading_bot.portfolio.accounting import FUNDING, SPOT_BORROW
from trading_bot.portfolio.records import AttemptRecord
from trading_bot.portfolio.valuation import ExecutableExit, MarkReader, position_value_usd


@dataclass(slots=True)
class PortfolioState:
    """The book, valued at one instant."""

    captured_at: datetime
    cash_usd: Decimal
    gross_exposure_usd: Decimal = Decimal(0)
    net_exposure_usd: Decimal = Decimal(0)
    position_value_usd: Decimal | None = None
    unrealized_pnl_usd: Decimal | None = None
    realized_pnl_usd: Decimal = Decimal(0)
    open_positions: int = 0
    unvalued_positions: int = 0
    unpaired_positions: int = 0
    valuation_status: ValuationStatus = ValuationStatus.COMPLETE
    #: Mark and unrealized P&L per position, for ``positions.mark_price``.
    marks: dict[int, tuple[Decimal, Decimal]] = field(default_factory=dict)
    #: Per-strategy open P&L. ``None`` means at least one leg in that strategy
    #: could not be marked, so its aggregate must not be invented as zero.
    strategy_unrealized_pnl_usd: dict[str, Decimal | None] = field(default_factory=dict)
    #: Carry costs omitted from open-position value.
    unmeasured_pnl: tuple[str, ...] = ()
    strategy_unmeasured_pnl: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def equity_usd(self) -> Decimal | None:
        if self.position_value_usd is None:
            return None
        return self.cash_usd + self.position_value_usd

    def as_row(self, scope: RunScope) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at,
            **scope.values(),
            "cash_usd": self.cash_usd,
            "gross_exposure_usd": self.gross_exposure_usd,
            "net_exposure_usd": self.net_exposure_usd,
            "position_value_usd": self.position_value_usd,
            "equity_usd": self.equity_usd,
            "open_positions": self.open_positions,
            "realized_pnl_usd": self.realized_pnl_usd,
            "unrealized_pnl_usd": self.unrealized_pnl_usd,
            "valuation_status": self.valuation_status,
            "unvalued_positions": self.unvalued_positions,
            "unpaired_positions": self.unpaired_positions,
        }


def value_book(
    attempts: Sequence[AttemptRecord],
    marks: MarkReader,
    *,
    cash_usd: Decimal,
    realized_pnl_usd: Decimal,
    captured_at: datetime,
) -> PortfolioState:
    """Value every open leg from the books as they are now."""
    state = PortfolioState(
        captured_at=captured_at, cash_usd=cash_usd, realized_pnl_usd=realized_pnl_usd
    )
    value = Decimal(0)
    unrealized = Decimal(0)
    for attempt in attempts:
        state.strategy_unrealized_pnl_usd.setdefault(attempt.strategy, Decimal(0))
        state.strategy_unmeasured_pnl.setdefault(attempt.strategy, ())
        if attempt.is_unpaired:
            state.unpaired_positions += 1
        for leg in attempt.live_legs:
            missing = (
                SPOT_BORROW
                if leg.market_type is MarketType.SPOT and leg.side is Side.SELL
                else FUNDING
                if leg.market_type is MarketType.PERPETUAL
                else None
            )
            if missing is not None:
                if missing not in state.unmeasured_pnl:
                    state.unmeasured_pnl = (*state.unmeasured_pnl, missing)
                strategy_missing = state.strategy_unmeasured_pnl[attempt.strategy]
                if missing not in strategy_missing:
                    state.strategy_unmeasured_pnl[attempt.strategy] = (
                        *strategy_missing,
                        missing,
                    )
            state.open_positions += 1
            state.gross_exposure_usd += leg.entry_price * leg.open_quantity
            sign = Decimal(1) if leg.side is Side.BUY else Decimal(-1)
            state.net_exposure_usd += sign * leg.entry_price * leg.open_quantity
            priced: ExecutableExit = marks.executable_exit(
                leg.ref, entry_side=leg.side, quantity=leg.open_quantity
            )
            if not priced.is_priced or priced.price is None:
                # No honest mark. Counted, never guessed at.
                state.unvalued_positions += 1
                state.strategy_unrealized_pnl_usd[attempt.strategy] = None
                continue
            value += position_value_usd(
                leg.market_type,
                entry_side=leg.side,
                entry_price=leg.entry_price,
                quantity=leg.open_quantity,
                executable_price=priced.price,
            )
            leg_unrealized = sign * (priced.price - leg.entry_price) * leg.open_quantity
            unrealized += leg_unrealized
            strategy_unrealized = state.strategy_unrealized_pnl_usd[attempt.strategy]
            if strategy_unrealized is not None:
                state.strategy_unrealized_pnl_usd[attempt.strategy] = (
                    strategy_unrealized + leg_unrealized
                )
            state.marks[leg.position_id] = (priced.price, leg_unrealized)
    if state.open_positions == 0:
        state.position_value_usd = Decimal(0)
        state.unrealized_pnl_usd = Decimal(0)
        return state
    if state.unvalued_positions:
        # Equity is an account-wide total. If even one position cannot be
        # priced, publishing the sum of the others would silently understate
        # exposure and manufacture a point in the return series.
        state.valuation_status = ValuationStatus.UNAVAILABLE
        return state
    state.position_value_usd = value
    state.unrealized_pnl_usd = unrealized
    state.valuation_status = (
        ValuationStatus.DEGRADED if state.unpaired_positions else ValuationStatus.COMPLETE
    )
    return state
