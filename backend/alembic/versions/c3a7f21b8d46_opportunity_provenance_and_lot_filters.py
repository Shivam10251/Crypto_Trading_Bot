"""opportunity provenance, lot filters and unpriceable opportunities

Additive throughout. Nothing already stored is rewritten, and nothing that was
stored becomes unreadable:

- ``markets`` gains the quantity filters an order actually has to satisfy.
  Existing rows keep NULL until the market-data service next registers them
  from the venue, which it does on every start.
- ``opportunities`` gains the episode's peak timestamp, its sample count, the
  correctly named prices, and a JSONB ``evidence`` document. Rows written
  before this migration have NULL in all of them: they are legacy, and that
  is a fact about them rather than something to backfill with invention.
- Cost and net-edge columns become nullable so an opportunity nobody could
  price can be stored without a fabricated zero, and ``UNPRICEABLE`` joins the
  status vocabulary.
- ``exit_price`` becomes nullable and stops being written. It held the *sold
  leg's entry price* under a name that says exit. Legacy rows keep that value
  and that meaning; new rows put the sold leg's entry price in
  ``sell_entry_price`` and the real modelled exits in the ``*_unwind_price``
  columns.

Revision ID: c3a7f21b8d46
Revises: b7d41e2a9c3f
Create Date: 2026-09-12 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3a7f21b8d46"
down_revision: str | None = "b7d41e2a9c3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QUANTITY = sa.Numeric(precision=28, scale=12)
PRICE = sa.Numeric(precision=28, scale=12)
MONEY = sa.Numeric(precision=20, scale=8)
BPS = sa.Numeric(precision=14, scale=6)

# ``status`` is a VARCHAR(11) with no CHECK constraint (SQLAlchemy's
# native_enum=False does not create one by default), and "UNPRICEABLE" is
# exactly as long as the longest value already stored - "PAPER_TRADE" - so
# admitting it needs no DDL at all.

_MARKET_QUANTITY_COLUMNS = (
    "min_qty",
    "max_qty",
    "market_min_qty",
    "market_max_qty",
    "market_step_size",
)
_NULLABLE_COSTS = (
    ("estimated_fees_usd", MONEY),
    ("estimated_slippage_usd", MONEY),
    ("funding_cost_usd", MONEY),
    ("borrow_cost_usd", MONEY),
    ("other_costs_usd", MONEY),
    ("safety_buffer_usd", MONEY),
    ("net_edge_usd", MONEY),
)


def upgrade() -> None:
    # --- markets: the filters a market order has to satisfy ---------------
    for column in _MARKET_QUANTITY_COLUMNS:
        op.add_column("markets", sa.Column(column, QUANTITY, nullable=True))
        op.create_check_constraint(
            f"{column}_positive", "markets", f"{column} IS NULL OR {column} > 0"
        )

    # --- opportunities: when the stored moment actually happened ----------
    op.add_column(
        "opportunities", sa.Column("best_observed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "opportunities", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("opportunities", sa.Column("samples", sa.Integer(), nullable=True))

    # --- opportunities: prices whose names mean what they say -------------
    op.add_column("opportunities", sa.Column("sell_entry_price", PRICE, nullable=True))
    op.add_column("opportunities", sa.Column("buy_unwind_price", PRICE, nullable=True))
    op.add_column("opportunities", sa.Column("sell_unwind_price", PRICE, nullable=True))
    op.add_column("opportunities", sa.Column("requested_notional_usd", MONEY, nullable=True))
    op.alter_column("opportunities", "exit_price", existing_type=PRICE, nullable=True)
    op.drop_constraint("prices_positive", "opportunities", type_="check")
    op.create_check_constraint(
        "prices_positive",
        "opportunities",
        "entry_price > 0 "
        "AND (exit_price IS NULL OR exit_price > 0) "
        "AND (sell_entry_price IS NULL OR sell_entry_price > 0) "
        "AND (buy_unwind_price IS NULL OR buy_unwind_price > 0) "
        "AND (sell_unwind_price IS NULL OR sell_unwind_price > 0)",
    )

    # --- opportunities: the decision's own evidence -----------------------
    op.add_column(
        "opportunities",
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # --- opportunities: an unpriceable one is stored, never invented ------
    for column, kind in _NULLABLE_COSTS:
        op.alter_column("opportunities", column, existing_type=kind, nullable=True)
    op.alter_column("opportunities", "net_edge_bps", existing_type=BPS, nullable=True)
    op.drop_constraint("costs_non_negative", "opportunities", type_="check")
    op.create_check_constraint(
        "costs_non_negative",
        "opportunities",
        "COALESCE(estimated_fees_usd, 0) >= 0 "
        "AND COALESCE(estimated_slippage_usd, 0) >= 0 "
        "AND COALESCE(safety_buffer_usd, 0) >= 0",
    )
    # Every existing row is priced and none is UNPRICEABLE, so this holds for
    # the whole table the moment it is added.
    op.create_check_constraint(
        "unpriceable_has_no_net_edge",
        "opportunities",
        "(status = 'UNPRICEABLE') = (net_edge_bps IS NULL)",
    )


def downgrade() -> None:
    # Rows added since the upgrade may be UNPRICEABLE or have a NULL
    # exit_price, and the pre-upgrade schema cannot hold either. They are
    # deleted rather than given invented values - a fabricated zero edge is
    # exactly what this migration exists to prevent.
    op.execute("DELETE FROM opportunities WHERE status = 'UNPRICEABLE' OR exit_price IS NULL")

    op.drop_constraint("unpriceable_has_no_net_edge", "opportunities", type_="check")
    op.drop_constraint("costs_non_negative", "opportunities", type_="check")
    op.create_check_constraint(
        "costs_non_negative",
        "opportunities",
        "estimated_fees_usd >= 0 AND estimated_slippage_usd >= 0 AND safety_buffer_usd >= 0",
    )
    op.alter_column("opportunities", "net_edge_bps", existing_type=BPS, nullable=False)
    for column, kind in reversed(_NULLABLE_COSTS):
        op.alter_column("opportunities", column, existing_type=kind, nullable=False)

    op.drop_column("opportunities", "evidence")
    op.drop_constraint("prices_positive", "opportunities", type_="check")
    op.create_check_constraint(
        "prices_positive", "opportunities", "entry_price > 0 AND exit_price > 0"
    )
    op.alter_column("opportunities", "exit_price", existing_type=PRICE, nullable=False)
    op.drop_column("opportunities", "requested_notional_usd")
    op.drop_column("opportunities", "sell_unwind_price")
    op.drop_column("opportunities", "buy_unwind_price")
    op.drop_column("opportunities", "sell_entry_price")

    op.drop_column("opportunities", "samples")
    op.drop_column("opportunities", "last_seen_at")
    op.drop_column("opportunities", "best_observed_at")

    for column in reversed(_MARKET_QUANTITY_COLUMNS):
        op.drop_constraint(f"{column}_positive", "markets", type_="check")
        op.drop_column("markets", column)
