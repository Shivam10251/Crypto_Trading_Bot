"""backtest runs and run isolation

Phase 11. A backtest writes the same kinds of rows paper execution does -
opportunities, signals, risk decisions, orders, fills, positions, snapshots -
through the same stores. ``ExecutionMode.BACKTEST`` alone would not keep them
apart: two runs over the same week produce identical deterministic client
order ids, intent ids and snapshot instants, so they would either collide on
the unique constraints or silently aggregate together.

1. ``backtest_runs`` - the durable identity of one replay: status, dataset,
   requested and actual range, markets, configuration snapshot and hash, code
   revision, lifecycle timestamps, counts, warnings and unmeasured components.
2. ``backtest_run_id`` on every result table, ``ON DELETE CASCADE``, with a
   CHECK that ``mode = 'BACKTEST'`` exactly when it is set. ``signals`` has no
   mode; its run is always its opportunity's.
3. Unique constraints re-keyed by run. Where every key column is NOT NULL the
   run joins the constraint with ``NULLS NOT DISTINCT`` (PostgreSQL 15+), so
   rows outside any run keep deduplicating against each other. Two old
   constraints had nullable keys - and every order written before Phase 8's
   remediation has NULL ``execution_intent_id``/``signal_leg`` - so each became
   a pair of partial unique indexes that preserves its semantics exactly.
4. ``BACKTEST`` added to the execution-mode CHECK constraints ``b5d9e2c4a7f1``
   created.
5. ``order_books.bids_complete``/``asks_complete`` and ``funding_observations``
   - what replay needs to know whether a recorded walk ran out of *known*
   depth, and what a perpetual's funding actually was.

**Downgrade refuses to delete backtests silently.** If any run exists it stops
and says so; ``alembic -x purge_backtests=true downgrade ...`` deletes every
run (cascading to its artifacts) first. Nothing else is lost by a downgrade,
but ``funding_observations`` and the two book flags are dropped with their
data.

Revision ID: c8e1f3a5b9d2
Revises: b5d9e2c4a7f1
Create Date: 2026-09-13 10:48:02.551370+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "c8e1f3a5b9d2"
down_revision: str | None = "b5d9e2c4a7f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRICE = sa.Numeric(28, 12)
QUANTITY = sa.Numeric(28, 12)

MODES_BEFORE = ("THEORETICAL", "PAPER", "LIVE")
MODES_AFTER = ("THEORETICAL", "PAPER", "LIVE", "BACKTEST")
RUN_STATUSES = ("PENDING", "RUNNING", "COMPLETED", "INCOMPLETE", "FAILED", "CANCELLED")

#: Tables whose rows a backtest can produce, and whether each has ``mode``.
RUN_SCOPED: tuple[tuple[str, bool, str], ...] = (
    # (table, has mode, time column for the (run, time) index)
    ("opportunities", True, "detected_at"),
    ("signals", False, "generated_at"),
    ("orders", True, "created_at"),
    ("fills", True, "filled_at"),
    ("positions", True, "closed_at"),
    ("risk_events", True, "occurred_at"),
    ("portfolio_snapshots", True, "captured_at"),
    ("pnl_snapshots", True, "captured_at"),
)

RUN_INDEX_NAMES = {
    "opportunities": "ix_opportunities_run_detected",
    "signals": "ix_signals_run_generated",
    "orders": "ix_orders_run_created",
    "fills": "ix_fills_run_filled_at",
    "positions": "ix_positions_run_closed_at",
    "risk_events": "ix_risk_events_run_occurred",
    "portfolio_snapshots": "ix_portfolio_snapshots_run_captured",
    "pnl_snapshots": "ix_pnl_snapshots_run_captured",
}

#: (table, old name, old columns, new name, new columns) for NOT NULL keys.
REKEYED_CONSTRAINTS: tuple[tuple[str, str, list[str], str, list[str]], ...] = (
    (
        "orders",
        "mode_client_order_id",
        ["mode", "client_order_id"],
        "mode_run_client_order_id",
        ["mode", "backtest_run_id", "client_order_id"],
    ),
    (
        "risk_events",
        "mode_intent_event_type",
        ["mode", "intent_id", "event_type"],
        "mode_run_intent_event_type",
        ["mode", "backtest_run_id", "intent_id", "event_type"],
    ),
    (
        "portfolio_snapshots",
        "mode_captured_at",
        ["mode", "captured_at"],
        "mode_run_captured_at",
        ["mode", "backtest_run_id", "captured_at"],
    ),
    (
        "pnl_snapshots",
        "mode_captured_window_scope",
        ["mode", "captured_at", "window", "scope_key"],
        "mode_run_captured_window_scope",
        ["mode", "backtest_run_id", "captured_at", "window", "scope_key"],
    ),
)

#: (table, old constraint, old columns, live index, run index, key columns).
PARTITIONED_CONSTRAINTS: tuple[tuple[str, str, list[str], str, str, list[str]], ...] = (
    (
        "orders",
        "mode_execution_intent_leg",
        ["mode", "execution_intent_id", "signal_leg"],
        "ux_orders_live_intent_leg",
        "ux_orders_run_intent_leg",
        ["execution_intent_id", "signal_leg"],
    ),
    (
        "positions",
        "mode_attempt_market",
        ["mode", "attempt_id", "market_id"],
        "ux_positions_live_attempt_market",
        "ux_positions_run_attempt_market",
        ["attempt_id", "market_id"],
    ),
)


def _modes_check(values: Sequence[str]) -> str:
    return "mode IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    _create_backtest_runs()
    _create_funding_observations()
    op.add_column(
        "order_books",
        sa.Column("bids_complete", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "order_books",
        sa.Column("asks_complete", sa.Boolean(), server_default=sa.false(), nullable=False),
    )

    for table, has_mode, time_column in RUN_SCOPED:
        op.add_column(table, sa.Column("backtest_run_id", sa.BigInteger(), nullable=True))
        op.create_foreign_key(
            op.f(f"fk_{table}_backtest_run_id_backtest_runs"),
            table,
            "backtest_runs",
            ["backtest_run_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.create_index(RUN_INDEX_NAMES[table], table, ["backtest_run_id", time_column])
        if has_mode:
            op.drop_constraint(op.f(f"ck_{table}_execution_mode"), table, type_="check")
            op.create_check_constraint(
                op.f(f"ck_{table}_execution_mode"), table, _modes_check(MODES_AFTER)
            )
            op.create_check_constraint(
                op.f(f"ck_{table}_backtest_mode_has_run"),
                table,
                "(mode = 'BACKTEST') = (backtest_run_id IS NOT NULL)",
            )

    for table, old, _, new, columns in REKEYED_CONSTRAINTS:
        op.drop_constraint(old, table, type_="unique")
        op.create_unique_constraint(new, table, columns, postgresql_nulls_not_distinct=True)

    for table, old, _, live, run, keys in PARTITIONED_CONSTRAINTS:
        op.drop_constraint(old, table, type_="unique")
        op.create_index(
            live,
            table,
            ["mode", *keys],
            unique=True,
            postgresql_where=sa.text("backtest_run_id IS NULL"),
        )
        op.create_index(
            run,
            table,
            ["backtest_run_id", *keys],
            unique=True,
            postgresql_where=sa.text("backtest_run_id IS NOT NULL"),
        )

    # An episode uid is unique per run, and still globally unique outside one.
    op.drop_index("ix_opportunities_uid", table_name="opportunities")
    op.create_index("ix_opportunities_uid", "opportunities", ["uid"])
    op.create_unique_constraint(
        "run_opportunity_uid",
        "opportunities",
        ["backtest_run_id", "uid"],
        postgresql_nulls_not_distinct=True,
    )


def _create_backtest_runs() -> None:
    zero = sa.text("0")
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_uid", sa.UUID(), nullable=False),
        sa.Column(
            "status", sa.String(length=10), server_default=sa.text("'PENDING'"), nullable=False
        ),
        sa.Column("dataset_source", sa.String(length=64), nullable=False),
        sa.Column("dataset_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("requested_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actual_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("markets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("code_revision", sa.String(length=64), nullable=True),
        sa.Column("code_dirty", sa.Boolean(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("initialization_events", sa.BigInteger(), server_default=zero, nullable=False),
        sa.Column("events_accepted", sa.BigInteger(), server_default=zero, nullable=False),
        sa.Column("events_rejected", sa.BigInteger(), server_default=zero, nullable=False),
        sa.Column("events_replayed", sa.BigInteger(), server_default=zero, nullable=False),
        sa.Column("evaluations", sa.BigInteger(), server_default=zero, nullable=False),
        sa.Column("opportunities_recorded", sa.BigInteger(), server_default=zero, nullable=False),
        sa.Column("orders_recorded", sa.BigInteger(), server_default=zero, nullable=False),
        sa.Column("fills_recorded", sa.BigInteger(), server_default=zero, nullable=False),
        sa.Column("trades_completed", sa.BigInteger(), server_default=zero, nullable=False),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("dataset_issues", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("completeness", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in RUN_STATUSES) + ")",
            name=op.f("ck_backtest_runs_backtest_run_status"),
        ),
        sa.CheckConstraint(
            "requested_end > requested_start", name=op.f("ck_backtest_runs_requested_range_ordered")
        ),
        sa.CheckConstraint(
            "actual_start IS NULL OR actual_end IS NULL OR actual_end >= actual_start",
            name=op.f("ck_backtest_runs_actual_range_ordered"),
        ),
        sa.CheckConstraint(
            "(status IN ('COMPLETED', 'INCOMPLETE', 'FAILED', 'CANCELLED')) "
            "= (completed_at IS NOT NULL)",
            name=op.f("ck_backtest_runs_terminal_has_completed_at"),
        ),
        sa.CheckConstraint(
            "status = 'PENDING' OR started_at IS NOT NULL",
            name=op.f("ck_backtest_runs_started_has_start"),
        ),
        sa.CheckConstraint(
            "status <> 'FAILED' OR failure_reason IS NOT NULL",
            name=op.f("ck_backtest_runs_failed_has_reason"),
        ),
        sa.CheckConstraint(
            "initialization_events >= 0 AND events_accepted >= 0 AND events_rejected >= 0 "
            "AND events_replayed >= 0 AND evaluations >= 0 AND opportunities_recorded >= 0 "
            "AND orders_recorded >= 0 AND fills_recorded >= 0 AND trades_completed >= 0",
            name=op.f("ck_backtest_runs_counts_non_negative"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_runs")),
        sa.UniqueConstraint("run_uid", name="run_uid"),
    )
    op.create_index("ix_backtest_runs_status_created", "backtest_runs", ["status", "created_at"])
    op.create_index("ix_backtest_runs_config_hash", "backtest_runs", ["config_hash"])


def _create_funding_observations() -> None:
    op.create_table(
        "funding_observations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("market_id", sa.BigInteger(), nullable=False),
        sa.Column("mark_price", PRICE, nullable=False),
        sa.Column("index_price", PRICE, nullable=False),
        sa.Column("funding_rate", QUANTITY, nullable=False),
        sa.Column("next_funding_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("funding_interval_hours", sa.Integer(), nullable=True),
        sa.Column("local_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mark_price > 0 AND index_price > 0",
            name=op.f("ck_funding_observations_prices_positive"),
        ),
        sa.CheckConstraint(
            "funding_interval_hours IS NULL OR funding_interval_hours > 0",
            name=op.f("ck_funding_observations_interval_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["market_id"],
            ["markets.id"],
            name=op.f("fk_funding_observations_market_id_markets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_funding_observations")),
        sa.UniqueConstraint("market_id", "local_timestamp", name="market_observed_at"),
    )
    op.create_index("ix_funding_observations_local_ts", "funding_observations", ["local_timestamp"])


def downgrade() -> None:
    bind = op.get_bind()
    runs = bind.execute(sa.text("SELECT count(*) FROM backtest_runs")).scalar_one()
    if runs:
        purge = context.get_x_argument(as_dictionary=True).get("purge_backtests", "")
        if purge.lower() != "true":
            raise RuntimeError(
                f"refusing to downgrade: {runs} backtest run(s) and their artifacts would be "
                "deleted. Re-run with `alembic -x purge_backtests=true downgrade ...` to delete "
                "them deliberately."
            )
        # Cascades to every opportunity, signal, order, fill, position, risk
        # event and snapshot the runs produced.
        bind.execute(sa.text("DELETE FROM backtest_runs"))

    op.drop_constraint("run_opportunity_uid", "opportunities", type_="unique")
    op.drop_index("ix_opportunities_uid", table_name="opportunities")
    op.create_index("ix_opportunities_uid", "opportunities", ["uid"], unique=True)

    for table, old, old_columns, live, run, _ in reversed(PARTITIONED_CONSTRAINTS):
        op.drop_index(run, table_name=table)
        op.drop_index(live, table_name=table)
        op.create_unique_constraint(old, table, old_columns)

    for table, old, old_columns, new, _ in reversed(REKEYED_CONSTRAINTS):
        op.drop_constraint(new, table, type_="unique")
        op.create_unique_constraint(old, table, old_columns)

    for table, has_mode, _ in reversed(RUN_SCOPED):
        if has_mode:
            op.drop_constraint(op.f(f"ck_{table}_backtest_mode_has_run"), table, type_="check")
            op.drop_constraint(op.f(f"ck_{table}_execution_mode"), table, type_="check")
            op.create_check_constraint(
                op.f(f"ck_{table}_execution_mode"), table, _modes_check(MODES_BEFORE)
            )
        op.drop_index(RUN_INDEX_NAMES[table], table_name=table)
        op.drop_constraint(
            op.f(f"fk_{table}_backtest_run_id_backtest_runs"), table, type_="foreignkey"
        )
        op.drop_column(table, "backtest_run_id")

    op.drop_column("order_books", "asks_complete")
    op.drop_column("order_books", "bids_complete")
    op.drop_index("ix_funding_observations_local_ts", table_name="funding_observations")
    op.drop_table("funding_observations")
    op.drop_index("ix_backtest_runs_config_hash", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_status_created", table_name="backtest_runs")
    op.drop_table("backtest_runs")
