"""Backtest runs - the identity every replayed artifact belongs to.

``ExecutionMode.BACKTEST`` says a row came from replay; it cannot say *which*
replay. Two backtests over the same week produce orders with the same
deterministic client ids, snapshots at the same ``captured_at`` and risk
decisions with the same intent ids, so a mode-only schema would either refuse
the second run on its unique constraints or - worse - let the two aggregate
into one number that describes neither.

So every result table carries ``backtest_run_id``: NULL for THEORETICAL,
PAPER and LIVE rows, and the owning run for BACKTEST rows, with a CHECK
constraint making the two impossible to disagree. Unique constraints include
the run, and every store filters on it - see ``trading_bot.db.scope``.

Deleting a run deletes everything it produced (``ON DELETE CASCADE``); nothing
outside the run references its rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trading_bot.db.base import Base, RecordMixin
from trading_bot.db.models.enum_types import BACKTEST_RUN_STATUS
from trading_bot.db.models.enums import BacktestRunStatus

#: The CHECK every run-scoped table with a ``mode`` column carries.
BACKTEST_MODE_HAS_RUN = "(mode = 'BACKTEST') = (backtest_run_id IS NOT NULL)"


def backtest_run_fk() -> Mapped[int | None]:
    """The owning run of a replayed row; NULL for every live or paper row."""
    return mapped_column(BigInteger, ForeignKey("backtest_runs.id", ondelete="CASCADE"))


#: ``backtest_run_id`` made comparable: 0 outside any run, the run's id inside one.
RUN_KEY_MATCHES_RUN = "run_key = COALESCE(backtest_run_id, 0)"


#: Keeps ``run_key`` equal to ``COALESCE(backtest_run_id, 0)`` for every writer.
RUN_KEY_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION set_run_key() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.run_key := COALESCE(NEW.backtest_run_id, 0);
    RETURN NEW;
END
$$
"""
#: Tables carrying ``run_key``: every end of a run-scoped relationship.
RUN_KEYED_TABLES = (
    "opportunities",
    "signals",
    "risk_events",
    "orders",
    "positions",
    "fills",
    "pnl_snapshots",
    "backtest_funding_payments",
)


def run_key_trigger_sql(table: str) -> str:
    return (
        f"CREATE TRIGGER trg_{table}_run_key BEFORE INSERT OR UPDATE OF backtest_run_id, "
        f"run_key ON {table} FOR EACH ROW EXECUTE FUNCTION set_run_key()"
    )


def run_key_column() -> Mapped[int]:
    """The key every run-scoped relationship is declared on.

    ``backtest_run_id`` cannot join a foreign key by itself: it is NULL for
    paper and live rows, and a composite key with a NULL column is not
    checked at all (``MATCH SIMPLE``). ``run_key`` is never NULL, so a
    foreign key on ``(parent_id, run_key)`` makes a cross-run link - a paper
    fill on a backtest order, one run's order on another run's signal -
    impossible for any writer, not merely for the stores that filter by run.

    An ordinary column, not a generated one: PostgreSQL refuses ``ON DELETE
    SET NULL`` on a foreign key containing a generated column, and the
    nullable links need it. A ``BEFORE INSERT OR UPDATE`` trigger derives it
    from ``backtest_run_id`` for every writer - ORM, Core upserts, raw SQL -
    so no code sets it, and ``RUN_KEY_MATCHES_RUN`` backs the trigger up.
    """
    return mapped_column(BigInteger, nullable=False, server_default=text("0"))


def install_run_key_triggers(metadata: MetaData) -> None:
    """Attach the trigger DDL to ``create_all`` - the migration creates the same."""
    event.listen(metadata, "before_create", DDL(RUN_KEY_FUNCTION_SQL))  # type: ignore[no-untyped-call]
    for name in RUN_KEYED_TABLES:
        trigger = DDL(run_key_trigger_sql(name))  # type: ignore[no-untyped-call]
        event.listen(metadata.tables[name], "after_create", trigger)


def run_key_target() -> UniqueConstraint:
    """What a run-scoped foreign key refers to: ``(id, run_key)``."""
    return UniqueConstraint("id", "run_key")


def run_scoped_fk(column: str, parent: str, *, ondelete: str) -> ForeignKeyConstraint:
    """``(column, run_key) -> parent (id, run_key)``.

    A nullable link keeps its ``SET NULL`` behaviour, limited to the link
    column itself: ``run_key`` is generated and must never be nulled. A NULL
    link is not checked, exactly as before.
    """
    action = f"SET NULL ({column})" if ondelete == "SET NULL" else ondelete
    return ForeignKeyConstraint(
        [column, "run_key"], [f"{parent}.id", f"{parent}.run_key"], ondelete=action
    )


class BacktestRun(Base, RecordMixin):
    """One replay of recorded market data through the real pipeline."""

    __tablename__ = "backtest_runs"

    # Public identity, safe for the CLI and logs. The surrogate ``id`` is what
    # artifacts reference, because it is what the unique constraints index.
    run_uid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[BacktestRunStatus] = mapped_column(
        BACKTEST_RUN_STATUS,
        nullable=False,
        default=BacktestRunStatus.PENDING,
        server_default=text("'PENDING'"),
    )

    # --- what was replayed ------------------------------------------------
    # Which HistoricalDataSource implementation, e.g. "postgres".
    dataset_source: Mapped[str] = mapped_column(String(64), nullable=False)
    # SHA-256 over the run's complete validated input: reference data,
    # initialization observations, and every accepted and rejected row of the
    # whole requested range (``backtest.source``). NULL unless the run
    # finished COMPLETED or INCOMPLETE.
    dataset_fingerprint: Mapped[str | None] = mapped_column(String(64))
    requested_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requested_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # First and last event the replay applied; NULL until one was.
    actual_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ["binance:BTCUSDT:SPOT", ...], sorted.
    markets: Mapped[list[str]] = mapped_column(JSONB, nullable=False)

    # --- with what --------------------------------------------------------
    # Every configuration section the pipeline read, as resolved - secrets
    # excluded - and its SHA-256, so two runs can be compared without diffing.
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Git revision of the code, and whether the working tree was dirty. NULL
    # when it could not be determined - never a guess.
    code_revision: Mapped[str | None] = mapped_column(String(64))
    code_dirty: Mapped[bool | None] = mapped_column(Boolean)
    # SHA-256 over HEAD plus the complete tracked/untracked worktree content.
    # Unlike ``code_dirty``, this distinguishes two uncommitted implementations.
    code_worktree_hash: Mapped[str | None] = mapped_column(String(64))

    # --- lifecycle --------------------------------------------------------
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Wall-clock liveness of the process running it. A RUNNING row whose
    # heartbeat stopped was interrupted, and is marked FAILED on inspection:
    # replay state lives in memory, so an interrupted run cannot resume.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Written by ``trading-bot-backtest cancel``; the running process polls it.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)

    # --- what happened ----------------------------------------------------
    # Counter definitions: ``backtest.runs.RunProgress``.
    initialization_events: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    events_accepted: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    events_rejected: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    events_replayed: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    evaluations: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    opportunities_recorded: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    orders_recorded: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    fills_recorded: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    trades_completed: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    # Bounded list of human-readable warnings, most important first.
    warnings: Mapped[list[str] | None] = mapped_column(JSONB)
    # Counts and bounded samples of gaps, duplicates, regressions, corrupt
    # rows and carry expiries the replay detected.
    dataset_issues: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # The per-component verdict - dataset, execution model, accounting,
    # valuation, persistence - and whether performance is rankable
    # (``backtest.completeness``). NULL until the run is terminal.
    completeness: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint("run_uid", name="run_uid"),
        Index("ix_backtest_runs_status_created", "status", "created_at"),
        Index("ix_backtest_runs_config_hash", "config_hash"),
        CheckConstraint("requested_end > requested_start", name="requested_range_ordered"),
        CheckConstraint(
            "actual_start IS NULL OR actual_end IS NULL OR actual_end >= actual_start",
            name="actual_range_ordered",
        ),
        # A terminal run says when it ended, and only a terminal run does.
        CheckConstraint(
            "(status IN ('COMPLETED', 'INCOMPLETE', 'FAILED', 'CANCELLED')) "
            "= (completed_at IS NOT NULL)",
            name="terminal_has_completed_at",
        ),
        CheckConstraint("status = 'PENDING' OR started_at IS NOT NULL", name="started_has_start"),
        # A failure nobody can explain later is a bug, not a record.
        CheckConstraint(
            "status <> 'FAILED' OR failure_reason IS NOT NULL", name="failed_has_reason"
        ),
        CheckConstraint(
            "initialization_events >= 0 AND events_accepted >= 0 AND events_rejected >= 0 "
            "AND events_replayed >= 0 AND evaluations >= 0 AND opportunities_recorded >= 0 "
            "AND orders_recorded >= 0 AND fills_recorded >= 0 AND trades_completed >= 0",
            name="counts_non_negative",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<BacktestRun {self.run_uid} {self.status}>"


class BacktestFundingPayment(Base, RecordMixin):
    """One funding cash flow posted at its virtual settlement instant.

    Position-level ``funding_pnl_usd`` remains the final attribution used to
    score a closed trade.  These rows are the cash ledger: they preserve when
    the payment happened, make retries idempotent, and let open-position
    funding affect equity and risk before the position closes.
    """

    __tablename__ = "backtest_funding_payments"

    backtest_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False
    )
    run_key: Mapped[int] = run_key_column()
    position_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    market_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("markets.id", ondelete="RESTRICT"), nullable=False
    )
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 12), nullable=False)
    mark_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)

    __table_args__ = (
        CheckConstraint(RUN_KEY_MATCHES_RUN, name="run_key_matches_run"),
        run_scoped_fk("position_id", "positions", ondelete="CASCADE"),
        UniqueConstraint(
            "backtest_run_id",
            "position_id",
            "settled_at",
            name="run_position_funding_settlement",
        ),
        Index(
            "ix_backtest_funding_payments_run_settled",
            "backtest_run_id",
            "settled_at",
        ),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("mark_price > 0", name="mark_price_positive"),
        CheckConstraint("observed_at <= settled_at", name="observation_precedes_settlement"),
    )
