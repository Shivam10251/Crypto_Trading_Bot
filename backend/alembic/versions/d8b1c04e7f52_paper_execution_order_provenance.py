"""paper execution: order provenance and the shadow flag

Two additive columns on ``orders``, both needed before a paper fill can be
stored honestly:

- ``opportunity_uid`` closes a link the traceability chain promised and could
  not deliver. ``signal_id`` is only available once the episode closes, which
  is after the order was placed; the episode's uid is fixed the moment it
  opens, so an order can name the opportunity behind it straight away.
- ``is_shadow`` separates a deliberate measurement probe from a trade the
  strategy asked for. Measured live, no opportunity on this account has ever
  passed validation, so the only way to learn what a round trip costs is to
  simulate one on purpose - and a probe that could not be told apart from a
  real paper trade would corrupt every P&L question Phase 10 asks.

No existing row is read or written; both columns default for what is there.

Revision ID: d8b1c04e7f52
Revises: c3a7f21b8d46
Create Date: 2026-09-12 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8b1c04e7f52"
down_revision: str | None = "c3a7f21b8d46"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("opportunity_uid", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("is_shadow", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_index("ix_orders_opportunity_uid", "orders", ["opportunity_uid"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_orders_opportunity_uid", table_name="orders")
    op.drop_column("orders", "is_shadow")
    op.drop_column("orders", "opportunity_uid")
