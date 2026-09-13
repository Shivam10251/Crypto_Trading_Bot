"""Valuing the book and writing the two snapshot tables.

``equity = cash + position value``, and each half is derived rather than
remembered:

- **cash** is replayed from the durable fills - starting cash, plus every spot
  sale, minus every spot purchase, minus every fee that was paid in cash. It
  is not read out of the in-memory ``PaperAccount``, so a restarted process
  reproduces the same number instead of inheriting one.
- **position value** walks the current books for what flattening each open leg
  would actually fetch. A spot leg contributes its executable proceeds (or the
  cost of buying back a borrowed one); a perpetual leg contributes only its
  unrealized P&L, because its margin was reserved against cash rather than
  spent. See ``valuation.position_value_usd``.

**A snapshot never uses a stale mark.** If any open leg cannot be priced from
a synchronised book the row is ``UNAVAILABLE`` with NULL position value and
NULL equity, and the equity curve skips it. A partial total is not equity.

**Idempotent by key.** ``captured_at`` is floored to the snapshot interval, so
a retry or a restart inside the same interval upserts the row it already
wrote. P&L rows key on ``(mode, captured_at, window, scope_key)`` for the same
reason.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, case, func, select
from sqlalchemy.dialects.postgresql import insert

from trading_bot.core.logging import get_logger
from trading_bot.db.models import Fill as FillRow
from trading_bot.db.models import Market as MarketRow
from trading_bot.db.models import Order as OrderRow
from trading_bot.db.models import PnlSnapshot as PnlSnapshotRow
from trading_bot.db.models import PortfolioSnapshot as PortfolioSnapshotRow
from trading_bot.db.models import Position as PositionRow
from trading_bot.db.models.enums import ExecutionMode, MarketType, Side, ValuationStatus
from trading_bot.execution.models import OrderIntent
from trading_bot.portfolio import accounting
from trading_bot.portfolio.accounting import FUNDING, SPOT_BORROW, PairedTrade
from trading_bot.portfolio.records import AttemptRecord
from trading_bot.portfolio.store import PortfolioStore, SessionFactory
from trading_bot.portfolio.valuation import ExecutableExit, MarkReader, position_value_usd

logger = get_logger(__name__)

#: Window names written to ``pnl_snapshots.window``.
WINDOW_DAY = "1d"
WINDOW_SESSION = "session"
WINDOW_ALL = "all"

PORTFOLIO_SCOPE = "portfolio"


def scope_key(strategy: str | None = None, position_id: int | None = None) -> str:
    if position_id is not None:
        return f"position:{position_id}"
    if strategy is not None:
        return f"strategy:{strategy}"
    return PORTFOLIO_SCOPE


def floor_to(moment: datetime, interval: timedelta) -> datetime:
    """Align a timestamp to the snapshot cadence, so retries share a key."""
    seconds = int(interval.total_seconds())
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = int((moment.astimezone(UTC) - epoch).total_seconds())
    return epoch + timedelta(seconds=elapsed - elapsed % max(1, seconds))


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

    def as_row(self, mode: ExecutionMode) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at,
            "mode": mode,
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


@dataclass(frozen=True, slots=True)
class Window:
    """One reporting window, and the equity curve inside it."""

    name: str
    start: datetime | None
    end: datetime


class SnapshotWriter:
    """Builds and persists both snapshot tables, idempotently."""

    def __init__(
        self,
        store: PortfolioStore,
        session_factory: SessionFactory,
        *,
        initial_cash_usd: Decimal,
        fees_paid_in_cash: bool,
        interval: timedelta,
        min_return_observations: int,
        risk_free_rate_annual_pct: float,
    ) -> None:
        self._store = store
        self._session_factory = session_factory
        self._initial_cash = initial_cash_usd
        self._fees_in_cash = fees_paid_in_cash
        self._interval = interval
        self._minimum = min_return_observations
        self._risk_free = risk_free_rate_annual_pct

    @property
    def mode(self) -> ExecutionMode:
        return self._store.mode

    # --- cash -----------------------------------------------------------

    def _cash_query(self) -> Select[tuple[Any, Any, Any, Any]]:
        spot_flow = func.sum(
            case(
                (
                    (MarketRow.market_type == MarketType.SPOT) & (OrderRow.side == Side.BUY),
                    -FillRow.price * FillRow.quantity,
                ),
                (
                    (MarketRow.market_type == MarketType.SPOT) & (OrderRow.side == Side.SELL),
                    FillRow.price * FillRow.quantity,
                ),
                else_=0,
            )
        )
        perpetual_realized = func.sum(
            case(
                (
                    (MarketRow.market_type == MarketType.PERPETUAL)
                    & (OrderRow.intent == OrderIntent.CLOSE.value)
                    & (PositionRow.side == Side.BUY),
                    (FillRow.price - PositionRow.entry_price) * FillRow.quantity,
                ),
                (
                    (MarketRow.market_type == MarketType.PERPETUAL)
                    & (OrderRow.intent == OrderIntent.CLOSE.value)
                    & (PositionRow.side == Side.SELL),
                    (PositionRow.entry_price - FillRow.price) * FillRow.quantity,
                ),
                else_=0,
            )
        )
        # Fee asset is durable evidence. Current configuration is used only
        # for legacy rows that predate fee-asset recording; changing the fee
        # preference must not rewrite historical cash.
        cash_fees = func.sum(
            case(
                (FillRow.fee_asset == "BNB", 0),
                (FillRow.fee_asset.is_not(None), FillRow.fee_usd),
                else_=FillRow.fee_usd if self._fees_in_cash else 0,
            )
        )
        bnb_fee_cases: list[tuple[Any, Any]] = [(FillRow.fee_asset == "BNB", FillRow.fee_usd)]
        if not self._fees_in_cash:
            bnb_fee_cases.append((FillRow.fee_asset.is_(None), FillRow.fee_usd))
        bnb_fees = func.sum(case(*bnb_fee_cases, else_=0))
        return (
            select(spot_flow, perpetual_realized, cash_fees, bnb_fees)
            .join(OrderRow, OrderRow.id == FillRow.order_id)
            .join(MarketRow, MarketRow.id == OrderRow.market_id)
            .outerjoin(PositionRow, PositionRow.id == FillRow.position_id)
            .where(
                FillRow.mode == self.mode,
                # Probes never moved this account's money.
                OrderRow.is_shadow.is_(False),
            )
        )

    async def balances(self) -> tuple[Decimal, Decimal]:
        """Replay cash from every durable fill.

        Perpetual entries move no cash: their margin is reserved against it.
        Their signed price P&L settles into cash on close. Fees follow each
        fill's recorded asset, not today's configuration. The second return
        value is durable BNB-denominated fee value, used to restore that
        wallet.
        """
        async with self._session_factory() as session:
            spot_flow, perpetual_realized, cash_fees, bnb_fees = (
                await session.execute(self._cash_query())
            ).one()
        cash = self._initial_cash + (spot_flow or Decimal(0)) + (perpetual_realized or Decimal(0))
        cash -= cash_fees or Decimal(0)
        return cash, bnb_fees or Decimal(0)

    async def cash(self) -> Decimal:
        cash, _ = await self.balances()
        return cash

    # --- writing --------------------------------------------------------

    async def write_portfolio(self, state: PortfolioState) -> None:
        values = state.as_row(self.mode)
        async with self._session_factory() as session:
            statement = insert(PortfolioSnapshotRow).values(values)
            await session.execute(
                statement.on_conflict_do_update(
                    constraint="mode_captured_at",
                    set_={
                        key: getattr(statement.excluded, key)
                        for key in values
                        if key not in {"mode", "captured_at"}
                    },
                )
            )

    async def equity_curve(self, window: Window) -> list[tuple[datetime, Decimal]]:
        """Usable equity points in a window, oldest first.

        Rows whose valuation was UNAVAILABLE carry NULL equity and are absent
        here rather than interpolated: a gap in the curve is a gap, and
        filling it would invent a period the system never measured.
        """
        conditions = [
            PortfolioSnapshotRow.mode == self.mode,
            PortfolioSnapshotRow.equity_usd.is_not(None),
            PortfolioSnapshotRow.captured_at <= window.end,
        ]
        if window.start is not None:
            conditions.append(PortfolioSnapshotRow.captured_at >= window.start)
        async with self._session_factory() as session:
            rows = await session.execute(
                select(PortfolioSnapshotRow.captured_at, PortfolioSnapshotRow.equity_usd)
                .where(*conditions)
                .order_by(PortfolioSnapshotRow.captured_at)
            )
            return [(at, equity) for at, equity in rows if equity is not None]

    def pnl_row(
        self,
        *,
        window: Window,
        captured_at: datetime,
        trades: Sequence[PairedTrade],
        curve: Sequence[tuple[datetime, Decimal]],
        strategy: str | None = None,
        position_id: int | None = None,
        unrealized_pnl_usd: Decimal | None = Decimal(0),
        equity_usd: Decimal | None = None,
        additional_unmeasured: Sequence[str] = (),
    ) -> dict[str, Any]:
        """One ``pnl_snapshots`` row from completed paired trades."""
        results = [trade.realized_pnl_usd for trade in trades]
        statistics = accounting.summarise_trades(results)
        unmeasured: list[str] = []
        for trade in trades:
            for component in trade.unmeasured:
                if component not in unmeasured:
                    unmeasured.append(component)
        for component in additional_unmeasured:
            if component not in unmeasured:
                unmeasured.append(component)
        equity = [value for _, value in curve]
        sample = accounting.sample_returns(list(curve), interval=self._interval)
        risk_free = _per_period_risk_free(self._risk_free, self._interval)
        sharpe = (
            accounting.sharpe_ratio(sample, risk_free_per_period=risk_free, minimum=self._minimum)
            if sample is not None
            else None
        )
        sortino = (
            accounting.sortino_ratio(sample, risk_free_per_period=risk_free, minimum=self._minimum)
            if sample is not None
            else None
        )
        return {
            "captured_at": captured_at,
            "mode": self.mode,
            "strategy": strategy,
            "position_id": position_id,
            "scope_key": scope_key(strategy, position_id),
            "window": window.name,
            "window_start": window.start,
            "window_end": window.end,
            "realized_pnl_usd": statistics.net_pnl_usd,
            "unrealized_pnl_usd": unrealized_pnl_usd,
            "fees_usd": sum((trade.fees_usd for trade in trades), Decimal(0)),
            "slippage_usd": sum((trade.slippage_usd for trade in trades), Decimal(0)),
            # NULL, not zero: nothing here measures either yet.
            "funding_pnl_usd": None,
            "borrow_cost_usd": None,
            "unmeasured_pnl": unmeasured or None,
            "equity_usd": equity_usd,
            "trade_count": statistics.trade_count,
            "winning_trades": statistics.winning_trades,
            "losing_trades": statistics.losing_trades,
            "average_trade_usd": statistics.average_trade_usd,
            "average_win_usd": statistics.average_win_usd,
            "average_loss_usd": statistics.average_loss_usd,
            "max_drawdown_usd": accounting.max_drawdown(equity),
            "win_rate": statistics.win_rate,
            "profit_factor": statistics.profit_factor,
            "expectancy_usd": (
                float(statistics.expectancy_usd) if statistics.expectancy_usd is not None else None
            ),
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "total_return_pct": accounting.total_return_pct(equity),
            "return_observations": len(sample.returns) if sample is not None else 0,
            # Always stated, even when both ratios are NULL: a reader has to
            # be able to see what sampling *would* have been annualised.
            "return_interval_seconds": int(self._interval.total_seconds()),
        }

    async def write_pnl(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        async with self._session_factory() as session:
            statement = insert(PnlSnapshotRow).values(list(rows))
            await session.execute(
                statement.on_conflict_do_update(
                    constraint="mode_captured_window_scope",
                    set_={
                        key: getattr(statement.excluded, key)
                        for key in rows[0]
                        if key not in {"mode", "captured_at", "window", "scope_key"}
                    },
                )
            )
        return len(rows)


def _per_period_risk_free(annual_pct: float, interval: timedelta) -> float:
    """Annual percentage rate as a simple rate for one sampling period."""
    if annual_pct <= 0:
        return 0.0
    periods = timedelta(days=365).total_seconds() / interval.total_seconds()
    return annual_pct / 100 / periods
