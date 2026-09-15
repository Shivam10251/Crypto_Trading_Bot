"""Opportunities and signals - the research dataset.

Every opportunity is stored, profitable or not. A dataset containing only the
trades we took cannot answer the questions that matter: how many opportunities
existed, how many survived fees, how many survived slippage, and how many were
actually executable.

Each row carries the ``market_data`` snapshots that produced it, so any decision
can be re-derived from the exact quotes behind it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trading_bot.db.base import Base, RecordMixin
from trading_bot.db.models.backtest import (
    BACKTEST_MODE_HAS_RUN,
    RUN_KEY_MATCHES_RUN,
    backtest_run_fk,
    run_key_column,
    run_key_target,
    run_scoped_fk,
)
from trading_bot.db.models.enum_types import (
    EXECUTION_MODE,
    OPPORTUNITY_STATUS,
    SIDE,
    SIGNAL_STATUS,
)
from trading_bot.db.models.enums import ExecutionMode, OpportunityStatus, Side, SignalStatus
from trading_bot.db.models.market import Market, MarketData
from trading_bot.db.models.types import BPS, MONEY, PRICE, QUANTITY

STRATEGY_NAME_LENGTH = 64


class Opportunity(Base, RecordMixin):
    """A price discrepancy the strategy noticed, with full cost accounting.

    Two legs are supported because the first strategy trades spot against
    perpetual futures: ``market`` is the leg being bought, ``secondary_market``
    the leg being sold (or vice versa per ``direction``).
    """

    __tablename__ = "opportunities"

    # Stable public identifier, safe to put in logs and dashboards.
    uid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, default=uuid.uuid4)
    # When the discrepancy was first observed - the episode's opening.
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # When the observation the economics below describe was taken. An episode
    # keeps its *best* moment, which is almost never its first, and writing
    # the opening time into both loses the only record of when the peak was.
    # NULL on rows written before this column existed.
    best_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Last observation of the episode; with detected_at it bounds the run.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Evaluations absorbed. One means the episode was seen exactly once.
    samples: Mapped[int | None] = mapped_column(Integer)
    strategy: Mapped[str] = mapped_column(String(STRATEGY_NAME_LENGTH), nullable=False)
    # THEORETICAL here: detection is independent of how it would be executed.
    # BACKTEST for an opportunity found while replaying history, which is
    # never research evidence about the live market.
    mode: Mapped[ExecutionMode] = mapped_column(EXECUTION_MODE, nullable=False)
    backtest_run_id: Mapped[int | None] = backtest_run_fk()
    run_key: Mapped[int] = run_key_column()

    # --- legs -------------------------------------------------------------
    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("markets.id", ondelete="RESTRICT"), nullable=False
    )
    secondary_market_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("markets.id", ondelete="RESTRICT")
    )
    direction: Mapped[Side] = mapped_column(SIDE, nullable=False)

    # --- provenance: the quotes this decision was based on ----------------
    # These two were designed to point at the ``market_data`` rows behind a
    # decision and have never been populated, because they cannot be: quotes
    # are *sampled* every 5 s rather than stored per evaluation, so no row
    # holds the quote a decision actually used, and retention deletes them
    # after 7 days while opportunities are kept forever. The decision's own
    # quotes and books live in ``evidence`` instead, which survives both.
    market_data_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("market_data.id", ondelete="SET NULL")
    )
    secondary_market_data_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("market_data.id", ondelete="SET NULL")
    )
    # Quotes, books, fills, venue filters, fee rates, the funding observation
    # and the cost model's assumptions - everything needed to re-derive this
    # row after retention has emptied the raw tables. NULL means the row
    # predates provenance and is not reproducible; it is never invented.
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # --- economics --------------------------------------------------------
    # The leg that was BOUGHT, at the average price walking its book gave.
    entry_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    # Legacy. This held the *sold* leg's entry price under a name that says
    # exit, which is a different thing entirely - the modelled unwind prices
    # below are the exits. No longer written; kept so old rows stay readable.
    exit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    # The leg that was SOLD, at the average price walking its book gave. This
    # is what ``exit_price`` was really holding.
    sell_entry_price: Mapped[Decimal | None] = mapped_column(PRICE)
    # What closing each leg would have fetched against the opposite side of
    # its own book at detection - modelled exits, priced, not assumed.
    buy_unwind_price: Mapped[Decimal | None] = mapped_column(PRICE)
    sell_unwind_price: Mapped[Decimal | None] = mapped_column(PRICE)
    # Rounded down to an increment both venues accept.
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    notional_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # What the strategy asked for before the venues' lot filters cut it down.
    requested_notional_usd: Mapped[Decimal | None] = mapped_column(MONEY)

    # Theoretical convergence edge, mid to mid. Not profit: realised price P&L
    # on a basis position is signed_quantity x (entry basis - exit basis), and
    # only an actual exit can supply the second term.
    gross_edge_bps: Mapped[Decimal] = mapped_column(BPS, nullable=False)
    gross_edge_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    # Cost breakdown, itemised so research can attribute lost edge. NULL - not
    # zero - when the opportunity could not be priced at all: a fabricated
    # zero would pollute every query asking what survived costs.
    estimated_fees_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    estimated_slippage_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    funding_cost_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    borrow_cost_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    other_costs_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    safety_buffer_usd: Mapped[Decimal | None] = mapped_column(MONEY)

    # NET = GROSS - fees - slippage - funding - borrow - other - buffer.
    # NULL for an UNPRICEABLE row, which is how "nobody could price this"
    # stays distinguishable from "priced, and it came to nothing".
    net_edge_bps: Mapped[Decimal | None] = mapped_column(BPS)
    net_edge_usd: Mapped[Decimal | None] = mapped_column(MONEY)

    # --- feasibility ------------------------------------------------------
    # Size actually available at the quoted levels.
    liquidity_usd: Mapped[Decimal | None] = mapped_column(MONEY)
    # Age of the data this decision used.
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # How long the discrepancy persisted; if shorter than round-trip latency,
    # it was never executable by this system.
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[OpportunityStatus] = mapped_column(
        OPPORTUNITY_STATUS, nullable=False, default=OpportunityStatus.DETECTED
    )
    # Populated for REJECTED/EXPIRED/FAILED - never leave a rejection unexplained.
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    market: Mapped[Market] = relationship(foreign_keys=[market_id], lazy="raise")
    secondary_market: Mapped[Market | None] = relationship(
        foreign_keys=[secondary_market_id], lazy="raise"
    )
    market_data: Mapped[MarketData | None] = relationship(
        foreign_keys=[market_data_id], lazy="raise"
    )
    signals: Mapped[list[Signal]] = relationship(
        back_populates="opportunity",
        primaryjoin="Opportunity.id == Signal.opportunity_id",
        foreign_keys="[Signal.opportunity_id]",
        lazy="raise",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # Links within one run only (``backtest.run_key_column``).
        CheckConstraint(RUN_KEY_MATCHES_RUN, name="run_key_matches_run"),
        run_key_target(),
        # Episode uids are deterministic under replay, so the same uid can
        # legitimately appear once per run. NULLS NOT DISTINCT keeps uids
        # outside any run globally unique, exactly as before.
        UniqueConstraint(
            "backtest_run_id",
            "uid",
            name="run_opportunity_uid",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_opportunities_uid", "uid"),
        Index("ix_opportunities_run_detected", "backtest_run_id", "detected_at"),
        CheckConstraint(BACKTEST_MODE_HAS_RUN, name="backtest_mode_has_run"),
        # Research queries: by time, by strategy, by outcome.
        Index("ix_opportunities_detected_at", "detected_at"),
        Index("ix_opportunities_strategy_detected", "strategy", "detected_at"),
        Index("ix_opportunities_status_detected", "status", "detected_at"),
        Index("ix_opportunities_market_detected", "market_id", "detected_at"),
        # "Which opportunities survived costs?" - the central research question.
        Index("ix_opportunities_net_edge", "net_edge_bps"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(
            "entry_price > 0 "
            "AND (exit_price IS NULL OR exit_price > 0) "
            "AND (sell_entry_price IS NULL OR sell_entry_price > 0) "
            "AND (buy_unwind_price IS NULL OR buy_unwind_price > 0) "
            "AND (sell_unwind_price IS NULL OR sell_unwind_price > 0)",
            name="prices_positive",
        ),
        CheckConstraint(
            "COALESCE(estimated_fees_usd, 0) >= 0 "
            "AND COALESCE(estimated_slippage_usd, 0) >= 0 "
            "AND COALESCE(safety_buffer_usd, 0) >= 0",
            name="costs_non_negative",
        ),
        # An unpriceable opportunity has no net edge, and a priced one always
        # has. The database enforces the distinction the research queries
        # depend on rather than trusting the writer to keep it.
        CheckConstraint(
            "(status = 'UNPRICEABLE') = (net_edge_bps IS NULL)",
            name="unpriceable_has_no_net_edge",
        ),
        CheckConstraint(
            "secondary_market_id IS NULL OR secondary_market_id <> market_id",
            name="legs_differ",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Opportunity {self.strategy} net={self.net_edge_bps}bps {self.status}>"


class Signal(Base, RecordMixin):
    """A strategy's intent to trade a validated opportunity.

    Separate from ``Opportunity`` because detection and the decision to act are
    different events: an opportunity can be detected and never acted on, and the
    gap between the two is exactly what research needs to measure.
    """

    __tablename__ = "signals"

    opportunity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Always the parent opportunity's run. Carried so a query over signals is
    # scoped by a column of its own rather than by remembering to join.
    backtest_run_id: Mapped[int | None] = backtest_run_fk()
    run_key: Mapped[int] = run_key_column()
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    strategy: Mapped[str] = mapped_column(String(STRATEGY_NAME_LENGTH), nullable=False)
    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("markets.id", ondelete="RESTRICT"), nullable=False
    )
    side: Mapped[Side] = mapped_column(SIDE, nullable=False)

    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    target_entry_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    # Where THIS leg would be closed: the modelled unwind against the other
    # side of its own book. Rows written before 2026-09-12 hold the *other
    # leg's entry* price here instead, which was never an exit of anything.
    target_exit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    expected_net_edge_bps: Mapped[Decimal] = mapped_column(BPS, nullable=False)

    status: Mapped[SignalStatus] = mapped_column(
        SIGNAL_STATUS, nullable=False, default=SignalStatus.GENERATED
    )
    # A signal acted on after this point is stale by definition.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)

    opportunity: Mapped[Opportunity] = relationship(
        back_populates="signals",
        primaryjoin="Opportunity.id == Signal.opportunity_id",
        foreign_keys=[opportunity_id],
        lazy="raise",
    )
    market: Mapped[Market] = relationship(lazy="raise")

    __table_args__ = (
        # Links within one run only (``backtest.run_key_column``).
        CheckConstraint(RUN_KEY_MATCHES_RUN, name="run_key_matches_run"),
        run_key_target(),
        run_scoped_fk("opportunity_id", "opportunities", ondelete="CASCADE"),
        UniqueConstraint("opportunity_id", "market_id", "side", name="opportunity_market_side"),
        Index("ix_signals_generated_at", "generated_at"),
        Index("ix_signals_opportunity", "opportunity_id"),
        Index("ix_signals_status_generated", "status", "generated_at"),
        Index("ix_signals_run_generated", "backtest_run_id", "generated_at"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("target_entry_price > 0", name="entry_price_positive"),
    )
