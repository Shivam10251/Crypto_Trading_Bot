"""Portfolio and P&L snapshots.

Both tables carry ``mode``, and every aggregate query must filter on it.
Theoretical edge, paper results and live results answer different questions:
mixing them produces a number that describes nothing.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trading_bot.db.base import Base, RecordMixin
from trading_bot.db.models.enum_types import EXECUTION_MODE
from trading_bot.db.models.enums import ExecutionMode
from trading_bot.db.models.execution import Position
from trading_bot.db.models.research import STRATEGY_NAME_LENGTH
from trading_bot.db.models.types import MONEY


class PortfolioSnapshot(Base, RecordMixin):
    """Point-in-time account state, used to rebuild the equity curve."""

    __tablename__ = "portfolio_snapshots"

    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mode: Mapped[ExecutionMode] = mapped_column(EXECUTION_MODE, nullable=False)

    cash_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # Sum of absolute position notionals - what the risk engine limits.
    gross_exposure_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    # Signed sum; near zero for a hedged spot/perp book.
    net_exposure_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    position_value_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    # cash + position value; the equity curve is built from this column.
    equity_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        # One snapshot per mode per instant.
        UniqueConstraint("mode", "captured_at", name="mode_captured_at"),
        Index("ix_portfolio_snapshots_mode_captured", "mode", "captured_at"),
        CheckConstraint("open_positions >= 0", name="open_positions_non_negative"),
        CheckConstraint("gross_exposure_usd >= 0", name="gross_exposure_non_negative"),
    )


class PnlSnapshot(Base, RecordMixin):
    """Performance metrics over a window.

    Ratios are ``double precision``: they are derived statistics, not money, so
    floating point is appropriate. Every monetary column stays NUMERIC.
    """

    __tablename__ = "pnl_snapshots"

    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mode: Mapped[ExecutionMode] = mapped_column(EXECUTION_MODE, nullable=False)
    # NULL = whole portfolio; set = one strategy's contribution.
    strategy: Mapped[str | None] = mapped_column(String(STRATEGY_NAME_LENGTH))
    # NULL = aggregate; set = a single position's P&L.
    position_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("positions.id", ondelete="SET NULL")
    )
    # Window this row summarises, e.g. "1d", "session", "all".
    window: Mapped[str] = mapped_column(String(16), nullable=False)

    realized_pnl_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    unrealized_pnl_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    fees_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    slippage_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    equity_usd: Mapped[Decimal | None] = mapped_column(MONEY)

    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    winning_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    losing_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    average_trade_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    average_win_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    average_loss_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    max_drawdown_usd: Mapped[Decimal | None] = mapped_column(MONEY)

    # Derived ratios; NULL until there is enough data to compute them honestly.
    win_rate: Mapped[float | None] = mapped_column(Float)
    profit_factor: Mapped[float | None] = mapped_column(Float)
    expectancy_usd: Mapped[float | None] = mapped_column(Float)
    sharpe_ratio: Mapped[float | None] = mapped_column(Float)
    sortino_ratio: Mapped[float | None] = mapped_column(Float)
    total_return_pct: Mapped[float | None] = mapped_column(Float)

    position: Mapped[Position | None] = relationship(lazy="raise")

    __table_args__ = (
        Index("ix_pnl_snapshots_mode_captured", "mode", "captured_at"),
        Index("ix_pnl_snapshots_strategy_captured", "strategy", "captured_at"),
        CheckConstraint("trade_count >= 0", name="trade_count_non_negative"),
        CheckConstraint(
            "winning_trades >= 0 AND losing_trades >= 0", name="trade_splits_non_negative"
        ),
        CheckConstraint(
            "winning_trades + losing_trades <= trade_count", name="trade_splits_within_count"
        ),
        CheckConstraint(
            "win_rate IS NULL OR (win_rate >= 0 AND win_rate <= 1)",
            name="win_rate_ratio",
        ),
    )
