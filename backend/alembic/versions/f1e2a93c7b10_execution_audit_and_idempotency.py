"""execution audit evidence and durable intent identity

Revision ID: f1e2a93c7b10
Revises: d8b1c04e7f52
Create Date: 2026-09-12 00:00:01.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1e2a93c7b10"
down_revision: str | None = "d8b1c04e7f52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "opportunity_market_side",
        "signals",
        ["opportunity_id", "market_id", "side"],
    )
    op.add_column("orders", sa.Column("execution_intent_id", sa.String(80)))
    op.add_column("orders", sa.Column("attempt_id", sa.String(64)))
    op.add_column("orders", sa.Column("signal_leg", sa.Integer()))
    op.add_column("orders", sa.Column("intent", sa.String(16)))
    op.add_column("orders", sa.Column("strategy", sa.String(64)))
    op.add_column("orders", sa.Column("expected_price", sa.Numeric(28, 12)))
    op.add_column("orders", sa.Column("terminal_latency_ms", sa.Integer()))
    op.add_column("orders", sa.Column("book_sequence", sa.BigInteger()))
    op.add_column("orders", sa.Column("book_local_timestamp", sa.DateTime(timezone=True)))
    op.add_column("orders", sa.Column("evidence", postgresql.JSONB()))
    op.create_unique_constraint(
        "mode_execution_intent_leg",
        "orders",
        ["mode", "execution_intent_id", "signal_leg"],
    )

    op.add_column("fills", sa.Column("fee_rate_bps", sa.Numeric(14, 6)))
    op.add_column("fills", sa.Column("fill_index", sa.Integer()))
    op.add_column("fills", sa.Column("book_sequence", sa.BigInteger()))
    op.add_column("fills", sa.Column("book_local_timestamp", sa.DateTime(timezone=True)))
    op.add_column("fills", sa.Column("levels", postgresql.JSONB()))
    op.create_unique_constraint("order_fill_index", "fills", ["order_id", "fill_index"])

    op.add_column("positions", sa.Column("opportunity_uid", postgresql.UUID(as_uuid=True)))
    op.add_column("positions", sa.Column("attempt_id", sa.String(64)))
    op.create_unique_constraint(
        "mode_attempt_market", "positions", ["mode", "attempt_id", "market_id"]
    )


def downgrade() -> None:
    op.drop_constraint("mode_attempt_market", "positions", type_="unique")
    op.drop_column("positions", "attempt_id")
    op.drop_column("positions", "opportunity_uid")
    op.drop_constraint("order_fill_index", "fills", type_="unique")
    for column in (
        "levels",
        "book_local_timestamp",
        "book_sequence",
        "fill_index",
        "fee_rate_bps",
    ):
        op.drop_column("fills", column)

    op.drop_constraint("mode_execution_intent_leg", "orders", type_="unique")
    for column in (
        "evidence",
        "book_local_timestamp",
        "book_sequence",
        "terminal_latency_ms",
        "expected_price",
        "intent",
        "strategy",
        "signal_leg",
        "attempt_id",
        "execution_intent_id",
    ):
        op.drop_column("orders", column)
    op.drop_constraint("opportunity_market_side", "signals", type_="unique")
