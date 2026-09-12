"""risk engine decision identity and idempotency

Phase 9 adds three columns ``risk_events`` needed from the start but never
had a writer for: ``intent_id`` (the stable execution-intent id a decision
shares with the order it approves or the work item it rejects, and the key a
retried evaluation converges on instead of duplicating), ``opportunity_uid``
(the same provenance-without-a-foreign-key pattern as ``orders.opportunity_uid``,
since a signal/opportunity row may not exist yet when the decision is made),
and ``is_shadow`` (so a probe's decision stays queryable but never mistaken
for one that governed real exposure).

Additive only: no existing column, table or constraint is touched, and
nothing here reads or rewrites another table. ``risk_events`` has never had a
writer before this phase, so backfilling is unnecessary in practice - but
``intent_id`` is backfilled defensively before being made ``NOT NULL`` so this
migration stays correct even against a database that already has rows.

Revision ID: 7e6346f5153d
Revises: a4c9e8126f30
Create Date: 2026-09-12 10:23:46.641469+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7e6346f5153d"
down_revision: str | None = "a4c9e8126f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("risk_events", sa.Column("opportunity_uid", sa.UUID(), nullable=True))
    op.add_column("risk_events", sa.Column("intent_id", sa.String(length=80), nullable=True))
    op.add_column(
        "risk_events",
        sa.Column("is_shadow", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    # Defensive backfill: every row written by this table's future writer
    # carries a real intent_id, but a NOT NULL added straight to an existing
    # table would fail loudly on any row from before this migration existed.
    op.execute("UPDATE risk_events SET intent_id = 'legacy:' || id WHERE intent_id IS NULL")
    op.alter_column("risk_events", "intent_id", nullable=False)
    op.create_index(
        "ix_risk_events_opportunity_uid", "risk_events", ["opportunity_uid"], unique=False
    )
    op.create_unique_constraint(
        "mode_intent_event_type", "risk_events", ["mode", "intent_id", "event_type"]
    )


def downgrade() -> None:
    op.drop_constraint("mode_intent_event_type", "risk_events", type_="unique")
    op.drop_index("ix_risk_events_opportunity_uid", table_name="risk_events")
    op.drop_column("risk_events", "is_shadow")
    op.drop_column("risk_events", "intent_id")
    op.drop_column("risk_events", "opportunity_uid")
