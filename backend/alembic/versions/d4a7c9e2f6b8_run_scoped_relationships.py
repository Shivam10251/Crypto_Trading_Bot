"""run-scoped relationships

Phase 11 hardening. ``c8e1f3a5b9d2`` stamped every result row with its run and
made every store filter on it, but the foreign keys between those rows still
named only a parent's id. Any writer could link a paper fill to a backtest
order, or one run's order to another run's signal, and the database accepted
it.

1. ``run_key BIGINT NOT NULL DEFAULT 0`` on every table that is either end of
   a provenance link, backfilled, maintained from ``backtest_run_id`` by a
   ``BEFORE INSERT OR UPDATE`` trigger for every writer, and held equal to
   ``COALESCE(backtest_run_id, 0)`` by a CHECK. It is 0 outside any run, so -
   unlike ``backtest_run_id`` - it can join a foreign key without ``MATCH
   SIMPLE`` silently skipping the check for paper rows. Not a generated
   column: PostgreSQL refuses ``ON DELETE SET NULL`` on a foreign key that
   contains one.
2. ``UNIQUE (id, run_key)`` on each referenced table.
3. Each provenance foreign key becomes ``(link, run_key) -> parent (id,
   run_key)``: signal -> opportunity, risk event -> signal, order -> signal,
   order -> risk event, fill -> order, fill -> position, position ->
   opportunity, P&L snapshot -> position. Deletion behaviour is unchanged: a
   ``CASCADE`` link still cascades, and a ``SET NULL`` link nulls only the link
   column (PostgreSQL 15+ column list), never the key. A NULL link is
   still allowed and still unchecked.

Existing rows all have ``backtest_run_id IS NULL`` on both ends (or a
consistent run, from ``c8e1f3a5b9d2`` onwards), so the new keys validate
without a data change; a row that violated one would stop the upgrade, which
is the point.

Revision ID: d4a7c9e2f6b8
Revises: c8e1f3a5b9d2
Create Date: 2026-09-13 15:10:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4a7c9e2f6b8"
down_revision: str | None = "c8e1f3a5b9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_KEY_MATCHES_RUN = "run_key = COALESCE(backtest_run_id, 0)"
# Frozen copies: a migration must not change when the models do.
RUN_KEY_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION set_run_key() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.run_key := COALESCE(NEW.backtest_run_id, 0);
    RETURN NEW;
END
$$
"""


def run_key_trigger_sql(table: str) -> str:
    return (
        f"CREATE TRIGGER trg_{table}_run_key BEFORE INSERT OR UPDATE OF backtest_run_id, "
        f"run_key ON {table} FOR EACH ROW EXECUTE FUNCTION set_run_key()"
    )


#: Tables that are the referenced end of a run-scoped link.
PARENTS = ("opportunities", "signals", "risk_events", "orders", "positions")
#: Every table that carries ``run_key``, parents first.
KEYED = (*PARENTS, "fills", "pnl_snapshots")

#: (child, link column, parent, ON DELETE)
LINKS: tuple[tuple[str, str, str, str], ...] = (
    ("signals", "opportunity_id", "opportunities", "CASCADE"),
    ("risk_events", "signal_id", "signals", "SET NULL"),
    ("orders", "signal_id", "signals", "SET NULL"),
    ("orders", "risk_event_id", "risk_events", "SET NULL"),
    ("fills", "order_id", "orders", "CASCADE"),
    ("fills", "position_id", "positions", "SET NULL"),
    ("positions", "opportunity_id", "opportunities", "SET NULL"),
    ("pnl_snapshots", "position_id", "positions", "SET NULL"),
)


def upgrade() -> None:
    op.execute(RUN_KEY_FUNCTION_SQL)
    for table in KEYED:
        op.add_column(
            table,
            sa.Column("run_key", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        )
        op.execute(
            f"UPDATE {table} SET run_key = backtest_run_id "  # noqa: S608 - fixed names
            "WHERE backtest_run_id IS NOT NULL"
        )
        op.execute(run_key_trigger_sql(table))
        op.create_check_constraint(
            op.f(f"ck_{table}_run_key_matches_run"), table, RUN_KEY_MATCHES_RUN
        )
    for table in PARENTS:
        op.create_unique_constraint(op.f(f"uq_{table}_id_run_key"), table, ["id", "run_key"])
    for child, column, parent, ondelete in LINKS:
        op.drop_constraint(op.f(f"fk_{child}_{column}_{parent}"), child, type_="foreignkey")
        op.create_foreign_key(
            op.f(f"fk_{child}_{column}_run_key_{parent}"),
            child,
            parent,
            [column, "run_key"],
            ["id", "run_key"],
            ondelete=f"SET NULL ({column})" if ondelete == "SET NULL" else ondelete,
        )


def downgrade() -> None:
    for child, column, parent, ondelete in reversed(LINKS):
        op.drop_constraint(op.f(f"fk_{child}_{column}_run_key_{parent}"), child, type_="foreignkey")
        op.create_foreign_key(
            op.f(f"fk_{child}_{column}_{parent}"),
            child,
            parent,
            [column],
            ["id"],
            ondelete=ondelete,
        )
    for table in reversed(PARENTS):
        op.drop_constraint(op.f(f"uq_{table}_id_run_key"), table, type_="unique")
    for table in reversed(KEYED):
        op.execute(f"DROP TRIGGER trg_{table}_run_key ON {table}")
        op.drop_constraint(op.f(f"ck_{table}_run_key_matches_run"), table, type_="check")
        op.drop_column(table, "run_key")
    op.execute("DROP FUNCTION set_run_key()")
