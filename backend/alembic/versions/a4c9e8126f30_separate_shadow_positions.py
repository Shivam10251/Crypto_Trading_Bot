"""separate hypothetical shadow positions from the paper portfolio

Revision ID: a4c9e8126f30
Revises: f1e2a93c7b10
Create Date: 2026-09-12 13:55:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4c9e8126f30"
down_revision: str | None = "f1e2a93c7b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("is_shadow", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("positions", "is_shadow")
