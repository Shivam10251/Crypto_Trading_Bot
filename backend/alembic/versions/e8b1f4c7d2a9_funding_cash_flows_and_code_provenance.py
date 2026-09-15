"""funding cash flows and dirty-worktree provenance

Phase 11 accounting hardening. Funding is a cash flow at the venue's
settlement instant, not an adjustment deferred until a position closes. The
new run-scoped ledger makes those payments durable and idempotent. A separate
worktree digest distinguishes uncommitted implementations that share HEAD.

Revision ID: e8b1f4c7d2a9
Revises: d4a7c9e2f6b8
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8b1f4c7d2a9"
down_revision: str | None = "d4a7c9e2f6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtest_runs", sa.Column("code_worktree_hash", sa.String(64)))
    op.create_table(
        "backtest_funding_payments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "backtest_run_id",
            sa.BigInteger(),
            sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_key", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("position_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "market_id",
            sa.BigInteger(),
            sa.ForeignKey("markets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.Numeric(28, 12), nullable=False),
        sa.Column("rate", sa.Numeric(20, 12), nullable=False),
        sa.Column("mark_price", sa.Numeric(28, 12), nullable=False),
        sa.Column("amount_usd", sa.Numeric(28, 8), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "run_key", name="uq_backtest_funding_payments_id_run_key"),
        sa.UniqueConstraint(
            "backtest_run_id",
            "position_id",
            "settled_at",
            name="run_position_funding_settlement",
        ),
        sa.CheckConstraint(
            "run_key = COALESCE(backtest_run_id, 0)",
            name=op.f("ck_backtest_funding_payments_run_key_matches_run"),
        ),
        sa.CheckConstraint(
            "quantity > 0", name=op.f("ck_backtest_funding_payments_quantity_positive")
        ),
        sa.CheckConstraint(
            "mark_price > 0", name=op.f("ck_backtest_funding_payments_mark_price_positive")
        ),
        sa.CheckConstraint(
            "observed_at <= settled_at",
            name=op.f("ck_backtest_funding_payments_observation_precedes_settlement"),
        ),
        sa.ForeignKeyConstraint(
            ["position_id", "run_key"],
            ["positions.id", "positions.run_key"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_backtest_funding_payments_run_settled",
        "backtest_funding_payments",
        ["backtest_run_id", "settled_at"],
    )
    op.execute(
        "CREATE TRIGGER trg_backtest_funding_payments_run_key "
        "BEFORE INSERT OR UPDATE OF backtest_run_id, run_key "
        "ON backtest_funding_payments FOR EACH ROW EXECUTE FUNCTION set_run_key()"
    )


def downgrade() -> None:
    op.drop_table("backtest_funding_payments")
    op.drop_column("backtest_runs", "code_worktree_hash")
