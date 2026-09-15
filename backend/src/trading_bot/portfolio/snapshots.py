"""Valuing the book and writing the two snapshot tables.

``equity = cash + position value``, and each half is derived rather than
remembered:

- **cash-equivalent equity** is replayed from durable fills and carrying cash
  flows - starting cash, spot flows, perpetual realized P&L and funding, minus
  every fee regardless of which wallet paid it. Actual cash and BNB-wallet
  consumption remain separate in ``balances()`` for account restoration.
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
wrote. P&L rows key on ``(mode, backtest_run_id, captured_at, window,
scope_key)`` for the same reason; the run is in both keys because two
backtests over one period snapshot the same instants.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, case, func, select
from sqlalchemy.dialects.postgresql import insert

from trading_bot.core.logging import get_logger
from trading_bot.db.models import BacktestFundingPayment
from trading_bot.db.models import Fill as FillRow
from trading_bot.db.models import Market as MarketRow
from trading_bot.db.models import Order as OrderRow
from trading_bot.db.models import PnlSnapshot as PnlSnapshotRow
from trading_bot.db.models import PortfolioSnapshot as PortfolioSnapshotRow
from trading_bot.db.models import Position as PositionRow
from trading_bot.db.models.enums import (
    ExecutionMode,
    MarketType,
    Side,
)
from trading_bot.db.scope import RunScope
from trading_bot.execution.models import OrderIntent
from trading_bot.portfolio.accounting import FUNDING, PairedTrade

# Re-exported: valuation moved to ``book_value`` in Phase 11, and callers that
# import both halves of a snapshot from here keep working.
from trading_bot.portfolio.book_value import PortfolioState
from trading_bot.portfolio.incremental import (
    CurveFigures,
    TradeFigures,
    curve_figures,
    trade_figures,
)
from trading_bot.portfolio.store import PortfolioStore, SessionFactory

logger = get_logger(__name__)

#: Window names written to ``pnl_snapshots.window``.
WINDOW_DAY = "1d"
WINDOW_SESSION = "session"
WINDOW_ALL = "all"

PORTFOLIO_SCOPE = "portfolio"

#: 500 rows x 32 columns stays far inside PostgreSQL's 32,767 parameters.
_PNL_ROWS_PER_STATEMENT = 500


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

    @property
    def scope(self) -> RunScope:
        return self._store.scope

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
                *self.scope.filters(FillRow.mode, FillRow.backtest_run_id),
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
            funding = Decimal(0)
            if self.scope.backtest_run_id is not None:
                funding = await session.scalar(
                    select(func.sum(BacktestFundingPayment.amount_usd)).where(
                        BacktestFundingPayment.backtest_run_id == self.scope.backtest_run_id
                    )
                ) or Decimal(0)
        cash = self._initial_cash + (spot_flow or Decimal(0)) + (perpetual_realized or Decimal(0))
        cash += funding or Decimal(0)
        cash -= cash_fees or Decimal(0)
        return cash, bnb_fees or Decimal(0)

    async def cash(self) -> Decimal:
        cash, bnb_fees = await self.balances()
        # BNB is a separate fee wallet, but consuming it is still an economic
        # expense. Excluding it would overstate equity, returns and ratios.
        return cash - bnb_fees

    # --- writing --------------------------------------------------------

    async def write_portfolio(self, state: PortfolioState) -> None:
        values = state.as_row(self.scope)
        async with self._session_factory() as session:
            statement = insert(PortfolioSnapshotRow).values(values)
            await session.execute(
                statement.on_conflict_do_update(
                    constraint="mode_run_captured_at",
                    set_={
                        key: getattr(statement.excluded, key)
                        for key in values
                        if key not in {"mode", "backtest_run_id", "captured_at"}
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
            *self.scope.filters(PortfolioSnapshotRow.mode, PortfolioSnapshotRow.backtest_run_id),
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

    @property
    def interval(self) -> timedelta:
        return self._interval

    @property
    def minimum_observations(self) -> int:
        return self._minimum

    @property
    def risk_free_per_period(self) -> float:
        return _per_period_risk_free(self._risk_free, self._interval)

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
        return self.row(
            window=window,
            captured_at=captured_at,
            trades=trade_figures(trades),
            curve=curve_figures(
                curve,
                interval=self._interval,
                risk_free_per_period=self.risk_free_per_period,
                minimum=self._minimum,
            ),
            strategy=strategy,
            position_id=position_id,
            unrealized_pnl_usd=unrealized_pnl_usd,
            equity_usd=equity_usd,
            additional_unmeasured=additional_unmeasured,
        )

    def row(
        self,
        *,
        window: Window,
        captured_at: datetime,
        trades: TradeFigures,
        curve: CurveFigures,
        strategy: str | None = None,
        position_id: int | None = None,
        unrealized_pnl_usd: Decimal | None = Decimal(0),
        equity_usd: Decimal | None = None,
        additional_unmeasured: Sequence[str] = (),
    ) -> dict[str, Any]:
        """The same row from figures - computed from history or folded in."""
        statistics = trades.statistics
        unmeasured = list(trades.unmeasured)
        for component in additional_unmeasured:
            if component not in unmeasured:
                unmeasured.append(component)
        return {
            "captured_at": captured_at,
            **self.scope.values(),
            "strategy": strategy,
            "position_id": position_id,
            "scope_key": scope_key(strategy, position_id),
            "window": window.name,
            "window_start": window.start,
            "window_end": window.end,
            "realized_pnl_usd": statistics.net_pnl_usd,
            "unrealized_pnl_usd": unrealized_pnl_usd,
            "fees_usd": trades.fees_usd,
            "slippage_usd": trades.slippage_usd,
            # NULL, not zero, unless every trade and open position here
            # measured it. Nothing measures spot borrow, so that is always NULL.
            "funding_pnl_usd": trades.funding_usd if FUNDING not in unmeasured else None,
            "borrow_cost_usd": None,
            "unmeasured_pnl": unmeasured or None,
            "equity_usd": equity_usd,
            "trade_count": statistics.trade_count,
            "winning_trades": statistics.winning_trades,
            "losing_trades": statistics.losing_trades,
            "average_trade_usd": statistics.average_trade_usd,
            "average_win_usd": statistics.average_win_usd,
            "average_loss_usd": statistics.average_loss_usd,
            "max_drawdown_usd": curve.max_drawdown_usd,
            "win_rate": statistics.win_rate,
            "profit_factor": statistics.profit_factor,
            "expectancy_usd": (
                float(statistics.expectancy_usd) if statistics.expectancy_usd is not None else None
            ),
            "sharpe_ratio": curve.sharpe_ratio,
            "sortino_ratio": curve.sortino_ratio,
            "total_return_pct": curve.total_return_pct,
            "return_observations": curve.return_observations,
            # Always stated, even when both ratios are NULL: a reader has to
            # be able to see what sampling *would* have been annualised.
            "return_interval_seconds": int(self._interval.total_seconds()),
        }

    async def write_pnl(self, rows: Sequence[dict[str, Any]]) -> int:
        """Upsert P&L rows in one transaction, in bounded statements.

        Bounded because PostgreSQL's protocol allows 32,767 bind parameters
        per statement and a row carries 32: the first snapshot after a start
        emits a row for every position ever closed, and past ~1,000 of them a
        single statement fails - on every later snapshot too, since nothing
        advances until one succeeds.
        """
        if not rows:
            return 0
        async with self._session_factory() as session:
            for start in range(0, len(rows), _PNL_ROWS_PER_STATEMENT):
                chunk = list(rows[start : start + _PNL_ROWS_PER_STATEMENT])
                statement = insert(PnlSnapshotRow).values(chunk)
                await session.execute(
                    statement.on_conflict_do_update(
                        constraint="mode_run_captured_window_scope",
                        set_={
                            key: getattr(statement.excluded, key)
                            for key in chunk[0]
                            if key
                            not in {"mode", "backtest_run_id", "captured_at", "window", "scope_key"}
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
