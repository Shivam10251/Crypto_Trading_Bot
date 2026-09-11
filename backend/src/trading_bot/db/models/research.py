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

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trading_bot.db.base import Base, RecordMixin
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
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    strategy: Mapped[str] = mapped_column(String(STRATEGY_NAME_LENGTH), nullable=False)
    # THEORETICAL here: detection is independent of how it would be executed.
    mode: Mapped[ExecutionMode] = mapped_column(EXECUTION_MODE, nullable=False)

    # --- legs -------------------------------------------------------------
    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("markets.id", ondelete="RESTRICT"), nullable=False
    )
    secondary_market_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("markets.id", ondelete="RESTRICT")
    )
    direction: Mapped[Side] = mapped_column(SIDE, nullable=False)

    # --- provenance: the quotes this decision was based on ----------------
    market_data_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("market_data.id", ondelete="SET NULL")
    )
    secondary_market_data_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("market_data.id", ondelete="SET NULL")
    )

    # --- economics --------------------------------------------------------
    entry_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    notional_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    gross_edge_bps: Mapped[Decimal] = mapped_column(BPS, nullable=False)
    gross_edge_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    # Cost breakdown, itemised so research can attribute lost edge.
    estimated_fees_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    estimated_slippage_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    funding_cost_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    borrow_cost_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    other_costs_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    safety_buffer_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)

    # NET = GROSS - fees - slippage - funding - borrow - other - buffer.
    net_edge_bps: Mapped[Decimal] = mapped_column(BPS, nullable=False)
    net_edge_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

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
        back_populates="opportunity", lazy="raise", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_opportunities_uid", "uid", unique=True),
        # Research queries: by time, by strategy, by outcome.
        Index("ix_opportunities_detected_at", "detected_at"),
        Index("ix_opportunities_strategy_detected", "strategy", "detected_at"),
        Index("ix_opportunities_status_detected", "status", "detected_at"),
        Index("ix_opportunities_market_detected", "market_id", "detected_at"),
        # "Which opportunities survived costs?" - the central research question.
        Index("ix_opportunities_net_edge", "net_edge_bps"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("entry_price > 0 AND exit_price > 0", name="prices_positive"),
        CheckConstraint(
            "estimated_fees_usd >= 0 AND estimated_slippage_usd >= 0 AND safety_buffer_usd >= 0",
            name="costs_non_negative",
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

    opportunity_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    strategy: Mapped[str] = mapped_column(String(STRATEGY_NAME_LENGTH), nullable=False)
    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("markets.id", ondelete="RESTRICT"), nullable=False
    )
    side: Mapped[Side] = mapped_column(SIDE, nullable=False)

    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    target_entry_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    target_exit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    expected_net_edge_bps: Mapped[Decimal] = mapped_column(BPS, nullable=False)

    status: Mapped[SignalStatus] = mapped_column(
        SIGNAL_STATUS, nullable=False, default=SignalStatus.GENERATED
    )
    # A signal acted on after this point is stale by definition.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)

    opportunity: Mapped[Opportunity] = relationship(back_populates="signals", lazy="raise")
    market: Mapped[Market] = relationship(lazy="raise")

    __table_args__ = (
        Index("ix_signals_generated_at", "generated_at"),
        Index("ix_signals_opportunity", "opportunity_id"),
        Index("ix_signals_status_generated", "status", "generated_at"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("target_entry_price > 0", name="entry_price_positive"),
    )
