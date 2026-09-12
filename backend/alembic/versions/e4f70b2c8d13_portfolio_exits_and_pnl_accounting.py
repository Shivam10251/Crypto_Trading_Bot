"""portfolio exits and P&L accounting

Phase 10. Additive only: every column added here is nullable or carries a
server default, no existing column changes type, and the two widenings
(``portfolio_snapshots.position_value_usd`` / ``equity_usd`` becoming
nullable) accept strictly more than before.

Three things the Phase 1 schema could not represent honestly:

1. **An exit.** ``positions`` held one price and one quantity, which is an
   entry. It now carries the closed quantity, the weighted exit price and
   notional, the exit's own fees and slippage, the signed price P&L, the
   durable claim two workers cannot both take, and the reason the exit fired.
   Reduce-only is a CHECK constraint here, not only a rule in the code that
   is supposed to respect it.
2. **A cash flow nobody measured.** Funding and spot borrow are real costs of
   holding these legs and nothing in this system can measure either yet.
   ``funding_pnl_usd`` / ``borrow_cost_usd`` are NULL rather than zero, and
   ``unmeasured_pnl`` names what a realized total is missing, so no row can
   present incomplete accounting as complete.
3. **A valuation that could not be made.** A snapshot that cannot price an
   open position from a synchronised book now says so
   (``valuation_status``, ``unvalued_positions``) and publishes NULL rather
   than a number derived from a stale mark.

Revision ID: e4f70b2c8d13
Revises: 7e6346f5153d
Create Date: 2026-09-12 21:05:12.418773+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4f70b2c8d13"
down_revision: str | None = "7e6346f5153d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(20, 8)
QUANTITY = sa.Numeric(28, 12)
PRICE = sa.Numeric(28, 12)


def upgrade() -> None:
    _upgrade_positions()
    _upgrade_portfolio_snapshots()
    _upgrade_pnl_snapshots()


def _upgrade_positions() -> None:
    op.add_column(
        "positions",
        sa.Column("closed_quantity", QUANTITY, nullable=False, server_default=sa.text("0")),
    )
    op.add_column("positions", sa.Column("exit_notional_usd", MONEY, nullable=True))
    op.add_column("positions", sa.Column("price_pnl_usd", MONEY, nullable=True))
    op.add_column(
        "positions",
        sa.Column("exit_fees_usd", MONEY, nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "positions",
        sa.Column("exit_slippage_usd", MONEY, nullable=False, server_default=sa.text("0")),
    )
    op.add_column("positions", sa.Column("funding_pnl_usd", MONEY, nullable=True))
    op.add_column("positions", sa.Column("borrow_cost_usd", MONEY, nullable=True))
    op.add_column(
        "positions", sa.Column("unmeasured_pnl", sa.dialects.postgresql.JSONB(), nullable=True)
    )
    op.add_column("positions", sa.Column("mark_price", PRICE, nullable=True))
    op.add_column("positions", sa.Column("marked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("positions", sa.Column("close_intent_id", sa.String(length=80), nullable=True))
    op.add_column("positions", sa.Column("close_claim_id", sa.String(length=64), nullable=True))
    op.add_column(
        "positions", sa.Column("close_claimed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "positions",
        sa.Column("close_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("positions", sa.Column("exit_reason", sa.String(length=32), nullable=True))
    # Rows written before this migration are entries that were never closed,
    # so closed_quantity 0 (the default) already describes them correctly and
    # there is nothing to backfill with invention.
    op.create_index("ix_positions_mode_closed_at", "positions", ["mode", "closed_at"])
    op.create_check_constraint("closed_quantity_non_negative", "positions", "closed_quantity >= 0")
    op.create_check_constraint("closed_within_quantity", "positions", "closed_quantity <= quantity")
    op.create_check_constraint("close_attempts_non_negative", "positions", "close_attempts >= 0")
    op.create_check_constraint(
        "exit_notional_non_negative",
        "positions",
        "exit_notional_usd IS NULL OR exit_notional_usd >= 0",
    )
    op.create_check_constraint(
        "closed_position_flat", "positions", "status <> 'CLOSED' OR closed_quantity = quantity"
    )


def _upgrade_portfolio_snapshots() -> None:
    op.add_column(
        "portfolio_snapshots",
        sa.Column("realized_pnl_usd", MONEY, nullable=False, server_default=sa.text("0")),
    )
    op.add_column("portfolio_snapshots", sa.Column("unrealized_pnl_usd", MONEY, nullable=True))
    op.add_column(
        "portfolio_snapshots",
        sa.Column(
            "valuation_status",
            sa.String(length=11),
            nullable=False,
            server_default=sa.text("'COMPLETE'"),
        ),
    )
    op.add_column(
        "portfolio_snapshots",
        sa.Column("unvalued_positions", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "portfolio_snapshots",
        sa.Column("unpaired_positions", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    # A valuation that could not be made must be able to say so instead of
    # publishing a zero. Widening only: every existing row stays valid.
    op.alter_column("portfolio_snapshots", "position_value_usd", nullable=True)
    op.alter_column("portfolio_snapshots", "equity_usd", nullable=True)
    op.create_check_constraint(
        "unvalued_positions_non_negative", "portfolio_snapshots", "unvalued_positions >= 0"
    )
    op.create_check_constraint(
        "unpaired_positions_non_negative", "portfolio_snapshots", "unpaired_positions >= 0"
    )
    op.create_check_constraint(
        "unavailable_valuation_has_no_equity",
        "portfolio_snapshots",
        "(valuation_status = 'UNAVAILABLE') = (equity_usd IS NULL)",
    )
    op.create_check_constraint(
        "equity_and_position_value_agree",
        "portfolio_snapshots",
        "(equity_usd IS NULL) = (position_value_usd IS NULL)",
    )


def _upgrade_pnl_snapshots() -> None:
    # A scope whose open positions cannot all be marked has no honest
    # aggregate unrealized P&L. Widening only; existing rows stay valid.
    op.alter_column("pnl_snapshots", "unrealized_pnl_usd", nullable=True)
    op.add_column(
        "pnl_snapshots",
        sa.Column(
            "scope_key",
            sa.String(length=96),
            nullable=False,
            server_default=sa.text("'portfolio'"),
        ),
    )
    op.add_column(
        "pnl_snapshots", sa.Column("window_start", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "pnl_snapshots", sa.Column("window_end", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("pnl_snapshots", sa.Column("funding_pnl_usd", MONEY, nullable=True))
    op.add_column("pnl_snapshots", sa.Column("borrow_cost_usd", MONEY, nullable=True))
    op.add_column(
        "pnl_snapshots", sa.Column("unmeasured_pnl", sa.dialects.postgresql.JSONB(), nullable=True)
    )
    op.add_column(
        "pnl_snapshots",
        sa.Column("return_observations", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "pnl_snapshots", sa.Column("return_interval_seconds", sa.Integer(), nullable=True)
    )
    # Defensive: this table has never had a writer, but a pre-existing row
    # would otherwise collide on the unique constraint below.
    op.execute(
        "UPDATE pnl_snapshots SET scope_key = CASE "
        "WHEN position_id IS NOT NULL THEN 'position:' || position_id "
        "WHEN strategy IS NOT NULL THEN 'strategy:' || strategy "
        "ELSE 'portfolio' END"
    )
    op.create_unique_constraint(
        "mode_captured_window_scope",
        "pnl_snapshots",
        ["mode", "captured_at", "window", "scope_key"],
    )
    op.create_check_constraint(
        "return_observations_non_negative", "pnl_snapshots", "return_observations >= 0"
    )
    op.create_check_constraint(
        "ratios_state_their_sampling",
        "pnl_snapshots",
        "(sharpe_ratio IS NULL AND sortino_ratio IS NULL) OR return_interval_seconds IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_pnl_snapshots_ratios_state_their_sampling", "pnl_snapshots")
    op.drop_constraint("ck_pnl_snapshots_return_observations_non_negative", "pnl_snapshots")
    op.drop_constraint("mode_captured_window_scope", "pnl_snapshots", type_="unique")
    for column in (
        "return_interval_seconds",
        "return_observations",
        "unmeasured_pnl",
        "borrow_cost_usd",
        "funding_pnl_usd",
        "window_end",
        "window_start",
        "scope_key",
    ):
        op.drop_column("pnl_snapshots", column)
    # Phase 10 may have written NULL for an unavailable valuation. Old code
    # cannot represent that state, so remove only those derived snapshot rows
    # rather than inventing a zero.
    op.execute("DELETE FROM pnl_snapshots WHERE unrealized_pnl_usd IS NULL")
    op.alter_column("pnl_snapshots", "unrealized_pnl_usd", nullable=False)

    op.drop_constraint(
        "ck_portfolio_snapshots_equity_and_position_value_agree", "portfolio_snapshots"
    )
    op.drop_constraint(
        "ck_portfolio_snapshots_unavailable_valuation_has_no_equity", "portfolio_snapshots"
    )
    op.drop_constraint(
        "ck_portfolio_snapshots_unpaired_positions_non_negative", "portfolio_snapshots"
    )
    op.drop_constraint(
        "ck_portfolio_snapshots_unvalued_positions_non_negative", "portfolio_snapshots"
    )
    # Restoring NOT NULL needs a value for the rows this phase may have
    # written as UNAVAILABLE. They are dropped rather than filled with a
    # fabricated equity: an invented number in the equity curve is worse than
    # a missing snapshot, and the rows they came from are all still present.
    op.execute("DELETE FROM portfolio_snapshots WHERE equity_usd IS NULL")
    op.alter_column("portfolio_snapshots", "equity_usd", nullable=False)
    op.alter_column("portfolio_snapshots", "position_value_usd", nullable=False)
    for column in (
        "unpaired_positions",
        "unvalued_positions",
        "valuation_status",
        "unrealized_pnl_usd",
        "realized_pnl_usd",
    ):
        op.drop_column("portfolio_snapshots", column)

    op.drop_constraint("ck_positions_closed_position_flat", "positions")
    op.drop_constraint("ck_positions_exit_notional_non_negative", "positions")
    op.drop_constraint("ck_positions_close_attempts_non_negative", "positions")
    op.drop_constraint("ck_positions_closed_within_quantity", "positions")
    op.drop_constraint("ck_positions_closed_quantity_non_negative", "positions")
    op.drop_index("ix_positions_mode_closed_at", table_name="positions")
    for column in (
        "exit_reason",
        "close_attempts",
        "close_claimed_at",
        "close_claim_id",
        "close_intent_id",
        "marked_at",
        "mark_price",
        "unmeasured_pnl",
        "borrow_cost_usd",
        "funding_pnl_usd",
        "exit_slippage_usd",
        "exit_fees_usd",
        "price_pnl_usd",
        "exit_notional_usd",
        "closed_quantity",
    ):
        op.drop_column("positions", column)
