"""Risk decisions and system events - the audit trail.

Two separate tables because they answer different questions. ``risk_events``
records what the risk engine decided and why; ``system_events`` records what the
infrastructure did (disconnects, gaps, errors). A trade gap in the research data
should always be explainable by a row in one of them.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trading_bot.db.base import Base, RecordMixin
from trading_bot.db.models.enum_types import (
    EXECUTION_MODE,
    RISK_DECISION,
    RISK_EVENT_TYPE,
    SEVERITY,
    SYSTEM_EVENT_TYPE,
)
from trading_bot.db.models.enums import (
    ExecutionMode,
    RiskDecision,
    RiskEventType,
    Severity,
    SystemEventType,
)
from trading_bot.db.models.research import STRATEGY_NAME_LENGTH, Signal
from trading_bot.db.models.types import MONEY


class RiskEvent(Base, RecordMixin):
    """One risk decision: approval, rejection or pause.

    ``limit_value`` / ``observed_value`` and the ``context`` snapshot make a
    decision reproducible after the fact. A rejection nobody can explain later
    is a bug, not a record.
    """

    __tablename__ = "risk_events"

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[RiskEventType] = mapped_column(RISK_EVENT_TYPE, nullable=False)
    decision: Mapped[RiskDecision] = mapped_column(RISK_DECISION, nullable=False)
    mode: Mapped[ExecutionMode] = mapped_column(EXECUTION_MODE, nullable=False)

    # The signal under evaluation; NULL for portfolio-level events such as a
    # daily loss limit or a kill switch.
    signal_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("signals.id", ondelete="SET NULL")
    )
    strategy: Mapped[str | None] = mapped_column(String(STRATEGY_NAME_LENGTH))

    # Which limit applied, and what was actually observed.
    limit_name: Mapped[str | None] = mapped_column(String(64))
    limit_value: Mapped[Decimal | None] = mapped_column(MONEY)
    observed_value: Mapped[Decimal | None] = mapped_column(MONEY)

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # Full input snapshot: exposure, position sizes, data age at decision time.
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    signal: Mapped[Signal | None] = relationship(lazy="raise")

    __table_args__ = (
        Index("ix_risk_events_occurred_at", "occurred_at"),
        Index("ix_risk_events_decision_occurred", "decision", "occurred_at"),
        Index("ix_risk_events_type_occurred", "event_type", "occurred_at"),
        Index("ix_risk_events_signal", "signal_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RiskEvent {self.event_type} {self.decision}>"


class SystemEvent(Base, RecordMixin):
    """Infrastructure event: connections, data gaps, errors, reconciliation."""

    __tablename__ = "system_events"

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[SystemEventType] = mapped_column(SYSTEM_EVENT_TYPE, nullable=False)
    severity: Mapped[Severity] = mapped_column(SEVERITY, nullable=False, default=Severity.INFO)
    # Emitting subsystem: "market_data", "strategy_engine", "risk_engine", ...
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_system_events_occurred_at", "occurred_at"),
        Index("ix_system_events_type_occurred", "event_type", "occurred_at"),
        Index("ix_system_events_severity_occurred", "severity", "occurred_at"),
        Index("ix_system_events_component_occurred", "component", "occurred_at"),
    )
