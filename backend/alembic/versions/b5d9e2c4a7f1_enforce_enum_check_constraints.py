"""enforce enum check constraints

Phase 11, before anything adds an enum value. ``enum_types._enum`` has always
documented enum columns as ``VARCHAR`` plus a database ``CHECK`` constraint,
but it never passed ``create_constraint=True``, so PostgreSQL enforced nothing:
the ORM's ``validate_strings`` refused a bad value, and any other writer - raw
SQL, a migration, a future service - could store ``'PAPR'`` in ``orders.mode``
and every aggregate filtering on the real value would silently lose the row.

This adds one constraint per enum column, named ``ck_<table>_<enum name>``,
exactly as ``create_constraint=True`` now makes the models describe.

**Existing data is validated first.** Adding a CHECK constraint to a table
holding an out-of-set value fails with a message naming neither the row nor
the value. Each column is scanned beforehand and the migration refuses with
the offending distinct values and their counts, so an operator can see what
to repair. Nothing is rewritten on their behalf: which value a corrupt row
should have held is not something a migration can know.

The value lists are frozen literals, not imported from ``enums``: a migration
has to describe the schema at *its* revision, and the enums will keep growing
(``c8e1f3a5b9d2`` adds ``BACKTEST`` and recreates the execution-mode ones).

Downgrade drops the constraints and nothing else.

Revision ID: b5d9e2c4a7f1
Revises: e4f70b2c8d13
Create Date: 2026-09-13 10:12:40.118902+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5d9e2c4a7f1"
down_revision: str | None = "e4f70b2c8d13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MARKET_TYPE = ("SPOT", "PERPETUAL", "FUTURE")
SIDE = ("BUY", "SELL")
ORDER_TYPE = ("MARKET", "LIMIT")
TIME_IN_FORCE = ("GTC", "IOC", "FOK")
ORDER_STATUS = (
    "PENDING",
    "SUBMITTED",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCELLED",
    "REJECTED",
    "EXPIRED",
    "FAILED",
)
OPPORTUNITY_STATUS = (
    "DETECTED",
    "VALIDATED",
    "REJECTED",
    "UNPRICEABLE",
    "PAPER_TRADE",
    "EXPIRED",
    "EXECUTED",
    "FAILED",
)
SIGNAL_STATUS = ("GENERATED", "APPROVED", "REJECTED", "EXECUTED", "EXPIRED")
RISK_DECISION = ("APPROVED", "REJECTED", "PAUSED")
RISK_EVENT_TYPE = (
    "PRE_TRADE_CHECK",
    "STALE_DATA",
    "LATENCY_EXCEEDED",
    "SLIPPAGE_EXCEEDED",
    "ORDER_SIZE_EXCEEDED",
    "POSITION_LIMIT_EXCEEDED",
    "EXPOSURE_LIMIT_EXCEEDED",
    "INSUFFICIENT_RESOURCES",
    "INCOMPLETE_MARKET_DATA",
    "SIGNAL_EXPIRED",
    "QUEUE_OVERLOAD",
    "DAILY_LOSS_LIMIT",
    "CONSECUTIVE_LOSSES",
    "ABNORMAL_EXECUTION",
    "KILL_SWITCH",
    "POSITION_EXIT",
    "REDUCE_ONLY_VIOLATION",
    "FAIL_CLOSED",
)
POSITION_STATUS = ("OPEN", "CLOSING", "CLOSED", "LIQUIDATED")
VALUATION_STATUS = ("COMPLETE", "DEGRADED", "UNAVAILABLE")
EXECUTION_MODE = ("THEORETICAL", "PAPER", "LIVE")
SYSTEM_EVENT_TYPE = (
    "STARTUP",
    "SHUTDOWN",
    "WS_CONNECTED",
    "WS_DISCONNECTED",
    "WS_RECONNECTED",
    "STALE_DATA",
    "DATA_GAP",
    "API_ERROR",
    "DATABASE_ERROR",
    "RECONCILIATION",
)
SEVERITY = ("INFO", "WARNING", "ERROR", "CRITICAL")

#: (table, column, enum name, allowed values) - every enum column in the schema.
ENUM_COLUMNS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("markets", "market_type", "market_type", MARKET_TYPE),
    ("trades_market", "aggressor_side", "side", SIDE),
    ("opportunities", "mode", "execution_mode", EXECUTION_MODE),
    ("opportunities", "direction", "side", SIDE),
    ("opportunities", "status", "opportunity_status", OPPORTUNITY_STATUS),
    ("signals", "side", "side", SIDE),
    ("signals", "status", "signal_status", SIGNAL_STATUS),
    ("orders", "mode", "execution_mode", EXECUTION_MODE),
    ("orders", "side", "side", SIDE),
    ("orders", "order_type", "order_type", ORDER_TYPE),
    ("orders", "time_in_force", "time_in_force", TIME_IN_FORCE),
    ("orders", "status", "order_status", ORDER_STATUS),
    ("fills", "mode", "execution_mode", EXECUTION_MODE),
    ("positions", "mode", "execution_mode", EXECUTION_MODE),
    ("positions", "side", "side", SIDE),
    ("positions", "status", "position_status", POSITION_STATUS),
    ("portfolio_snapshots", "mode", "execution_mode", EXECUTION_MODE),
    ("portfolio_snapshots", "valuation_status", "valuation_status", VALUATION_STATUS),
    ("pnl_snapshots", "mode", "execution_mode", EXECUTION_MODE),
    ("risk_events", "event_type", "risk_event_type", RISK_EVENT_TYPE),
    ("risk_events", "decision", "risk_decision", RISK_DECISION),
    ("risk_events", "mode", "execution_mode", EXECUTION_MODE),
    ("system_events", "event_type", "system_event_type", SYSTEM_EVENT_TYPE),
    ("system_events", "severity", "severity", SEVERITY),
)


def constraint_name(table: str, enum_name: str) -> str:
    return f"ck_{table}_{enum_name}"


def check_sql(column: str, values: Sequence[str]) -> str:
    """The same predicate ``create_constraint=True`` renders for the models."""
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


def invalid_values(table: str, column: str, values: Sequence[str]) -> list[tuple[str, int]]:
    """Distinct stored values the constraint would refuse, with their counts.

    The table and column names come from ``ENUM_COLUMNS`` above, never from
    input, and the values are bound parameters.
    """
    statement = sa.text(
        f"SELECT {column}, count(*) FROM {table} "  # noqa: S608 - fixed identifiers
        f"WHERE {column} IS NOT NULL AND NOT ({column} = ANY(:allowed)) "
        f"GROUP BY {column} ORDER BY {column}"
    ).bindparams(sa.bindparam("allowed", value=list(values), type_=sa.ARRAY(sa.String())))
    rows = op.get_bind().execute(statement).all()
    return [(str(value), int(count)) for value, count in rows]


def upgrade() -> None:
    problems = [
        f"{table}.{column}: " + ", ".join(f"{value!r} x{count}" for value, count in found)
        for table, column, _, values in ENUM_COLUMNS
        if (found := invalid_values(table, column, values))
    ]
    if problems:
        raise RuntimeError(
            "refusing to add enum CHECK constraints over rows that would violate them; "
            "repair these values first: " + "; ".join(problems)
        )
    for table, column, enum_name, values in ENUM_COLUMNS:
        op.create_check_constraint(
            op.f(constraint_name(table, enum_name)), table, check_sql(column, values)
        )


def downgrade() -> None:
    for table, _, enum_name, _ in reversed(ENUM_COLUMNS):
        op.drop_constraint(op.f(constraint_name(table, enum_name)), table, type_="check")
