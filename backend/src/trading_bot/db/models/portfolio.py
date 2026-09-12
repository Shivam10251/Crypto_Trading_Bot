"""Portfolio and P&L snapshots.

Both tables carry ``mode``, and every aggregate query must filter on it.
Theoretical edge, paper results and live results answer different questions:
mixing them produces a number that describes nothing.

Two honesty rules are enforced in the columns themselves rather than left to
the writer:

- **A valuation that could not be made is not a zero.** ``position_value_usd``
  and ``equity_usd`` are nullable, and ``valuation_status`` says whether the
  snapshot priced every open position from a synchronised book, priced some of
  them, or declined to publish a number at all.
- **An unmeasured cash flow is not a zero either.** ``funding_pnl_usd`` and
  ``borrow_cost_usd`` are NULL when nothing in this system could measure them,
  and ``unmeasured_pnl`` names exactly which components a realized total is
  missing - so no row can present incomplete accounting as a total.
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trading_bot.db.base import Base, RecordMixin
from trading_bot.db.models.enum_types import EXECUTION_MODE, VALUATION_STATUS
from trading_bot.db.models.enums import ExecutionMode, ValuationStatus
from trading_bot.db.models.execution import Position
from trading_bot.db.models.research import STRATEGY_NAME_LENGTH
from trading_bot.db.models.types import MONEY

#: ``pnl_snapshots.scope_key`` - "portfolio", "strategy:<name>" or
#: "position:<id>". A plain NOT NULL column rather than a nullable pair,
#: because a unique constraint over NULLs does not deduplicate in PostgreSQL
#: and snapshot idempotency has to be a constraint, not a convention.
SCOPE_KEY_LENGTH = 96


class PortfolioSnapshot(Base, RecordMixin):
    """Point-in-time account state, used to rebuild the equity curve."""

    __tablename__ = "portfolio_snapshots"

    # Aligned to the snapshot interval by the writer, so a retry or a restart
    # inside the same interval converges on this row rather than adding one.
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mode: Mapped[ExecutionMode] = mapped_column(EXECUTION_MODE, nullable=False)

    cash_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # Sum of absolute position notionals - what the risk engine limits.
    gross_exposure_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    # Signed sum; near zero for a hedged spot/perp book.
    net_exposure_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    # What the open book is worth if it were flattened into the books as they
    # are now - an executable walk, not a mid. NULL when valuation is
    # UNAVAILABLE: no mark is better than a stale one.
    position_value_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    # cash + position value; the equity curve is built from this column, and
    # a NULL is skipped by the curve rather than read as a flat period.
    equity_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Committed realized net P&L for this mode up to ``captured_at``, and the
    # mark-to-market on what is still open. Stored so the equity curve can be
    # decomposed without re-reading every fill.
    realized_pnl_usd: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=0, server_default=text("0")
    )
    unrealized_pnl_usd: Mapped[Decimal | None] = mapped_column(MONEY)

    valuation_status: Mapped[ValuationStatus] = mapped_column(
        VALUATION_STATUS,
        nullable=False,
        default=ValuationStatus.COMPLETE,
        server_default=text("'COMPLETE'"),
    )
    # Open positions this snapshot could not price from a synchronised book.
    unvalued_positions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Live positions whose paired leg is no longer open: real naked exposure,
    # counted so a dashboard and the health endpoint cannot miss it.
    unpaired_positions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    __table_args__ = (
        # One snapshot per mode per instant.
        UniqueConstraint("mode", "captured_at", name="mode_captured_at"),
        Index("ix_portfolio_snapshots_mode_captured", "mode", "captured_at"),
        CheckConstraint("open_positions >= 0", name="open_positions_non_negative"),
        CheckConstraint("gross_exposure_usd >= 0", name="gross_exposure_non_negative"),
        CheckConstraint("unvalued_positions >= 0", name="unvalued_positions_non_negative"),
        CheckConstraint("unpaired_positions >= 0", name="unpaired_positions_non_negative"),
        # An UNAVAILABLE valuation publishes no number; anything else does.
        CheckConstraint(
            "(valuation_status = 'UNAVAILABLE') = (equity_usd IS NULL)",
            name="unavailable_valuation_has_no_equity",
        ),
        CheckConstraint(
            "(equity_usd IS NULL) = (position_value_usd IS NULL)",
            name="equity_and_position_value_agree",
        ),
    )


class PnlSnapshot(Base, RecordMixin):
    """Performance metrics over a window.

    Ratios are ``double precision``: they are derived statistics, not money, so
    floating point is appropriate. Every monetary column stays NUMERIC.

    The window is stated, not implied: ``window`` names it ("1d", "session",
    "all") and ``window_start`` / ``window_end`` give its exact bounds, so a
    daily row says which UTC day it means instead of leaving a reader to infer
    it from ``captured_at``.
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
    # The non-null rendering of (strategy, position_id) that idempotency keys
    # on: "portfolio", "strategy:<name>" or "position:<id>".
    scope_key: Mapped[str] = mapped_column(
        String(SCOPE_KEY_LENGTH), nullable=False, server_default=text("'portfolio'")
    )
    # Window this row summarises, e.g. "1d", "session", "all".
    window: Mapped[str] = mapped_column(String(16), nullable=False)
    # Half-open [start, end). ``window_start`` is NULL for an all-time window,
    # which genuinely has no start.
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    realized_pnl_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    # NULL when any open position in this scope could not be valued. Zero is
    # reserved for a genuinely flat or fully valued zero-P&L scope.
    unrealized_pnl_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    fees_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    # Attribution only - already inside the fill prices, never subtracted
    # from realized P&L a second time.
    slippage_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    # NULL means "nothing here could measure it", never zero.
    funding_pnl_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    borrow_cost_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    # Component names missing from ``realized_pnl_usd``; empty or NULL means
    # the realized figure is complete for the trades it covers.
    unmeasured_pnl: Mapped[list[str] | None] = mapped_column(JSONB)
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
    # What the two ratios above were computed from. Sharpe annualised from an
    # unknown or irregular sampling interval is a number with no meaning, so
    # the interval and the observation count are stored alongside them and
    # both ratios stay NULL when there are not enough regular observations.
    return_observations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    return_interval_seconds: Mapped[int | None] = mapped_column(Integer)

    position: Mapped[Position | None] = relationship(lazy="raise")

    __table_args__ = (
        # Snapshot idempotency: one row per scope per window per instant, so a
        # retried flush or a restarted service converges instead of
        # double-counting the same window.
        UniqueConstraint(
            "mode", "captured_at", "window", "scope_key", name="mode_captured_window_scope"
        ),
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
        CheckConstraint("return_observations >= 0", name="return_observations_non_negative"),
        # A ratio without its sampling assumption is not auditable.
        CheckConstraint(
            "(sharpe_ratio IS NULL AND sortino_ratio IS NULL) "
            "OR return_interval_seconds IS NOT NULL",
            name="ratios_state_their_sampling",
        ),
    )
