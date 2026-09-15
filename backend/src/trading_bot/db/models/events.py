"""Risk decisions and system events - the audit trail.

Two separate tables because they answer different questions. ``risk_events``
records what the risk engine decided and why; ``system_events`` records what the
infrastructure did (disconnects, gaps, errors). A trade gap in the research data
should always be explainable by a row in one of them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    false,
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
    # The owning backtest run, so a replayed kill switch or loss halt governs
    # that run alone and never the paper service - or another run.
    backtest_run_id: Mapped[int | None] = backtest_run_fk()
    run_key: Mapped[int] = run_key_column()

    # The signal under evaluation; NULL for portfolio-level events such as a
    # daily loss limit or a kill switch.
    signal_id: Mapped[int | None] = mapped_column(BigInteger)
    # The opportunity this decision concerned, carried directly rather than by
    # foreign key: a signal/opportunity row may not exist yet when the
    # decision is made, the same reason ``orders.opportunity_uid`` exists.
    opportunity_uid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Stable identity of the decision itself - the same execution-intent id
    # carried by the order it approves or the work item it rejects. Makes a
    # retried evaluation idempotent instead of writing a second row.
    intent_id: Mapped[str] = mapped_column(String(80), nullable=False)
    # A shadow probe's decision, kept queryable but never mistaken for one
    # that governed real portfolio exposure.
    is_shadow: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    strategy: Mapped[str | None] = mapped_column(String(STRATEGY_NAME_LENGTH))

    # Which limit applied, and what was actually observed.
    limit_name: Mapped[str | None] = mapped_column(String(64))
    limit_value: Mapped[Decimal | None] = mapped_column(MONEY)
    observed_value: Mapped[Decimal | None] = mapped_column(MONEY)

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # Full input snapshot: exposure, position sizes, data age at decision time.
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    signal: Mapped[Signal | None] = relationship(
        primaryjoin="RiskEvent.signal_id == Signal.id", foreign_keys=[signal_id], lazy="raise"
    )

    __table_args__ = (
        # Links within one run only (``backtest.run_key_column``).
        CheckConstraint(RUN_KEY_MATCHES_RUN, name="run_key_matches_run"),
        run_key_target(),
        run_scoped_fk("signal_id", "signals", ondelete="SET NULL"),
        Index("ix_risk_events_occurred_at", "occurred_at"),
        Index("ix_risk_events_decision_occurred", "decision", "occurred_at"),
        Index("ix_risk_events_type_occurred", "event_type", "occurred_at"),
        Index("ix_risk_events_signal", "signal_id"),
        Index("ix_risk_events_opportunity_uid", "opportunity_uid"),
        # A retried evaluation of the same intent converges on one row per
        # gate rather than writing a duplicate every time it is retried.
        UniqueConstraint(
            "mode",
            "backtest_run_id",
            "intent_id",
            "event_type",
            name="mode_run_intent_event_type",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_risk_events_run_occurred", "backtest_run_id", "occurred_at"),
        CheckConstraint(BACKTEST_MODE_HAS_RUN, name="backtest_mode_has_run"),
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
