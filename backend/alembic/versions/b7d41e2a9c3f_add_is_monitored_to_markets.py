"""add is_monitored to markets

Revision ID: b7d41e2a9c3f
Revises: 9ef15a885330
Create Date: 2026-09-11 09:30:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d41e2a9c3f"
down_revision: str | None = "9ef15a885330"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows predate market selection: none is monitored until the
    # service next starts and records its choice.
    op.add_column(
        "markets",
        sa.Column("is_monitored", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("markets", "is_monitored")
