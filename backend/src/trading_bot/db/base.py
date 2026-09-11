"""Declarative base and shared column conventions.

Conventions live here so every table inherits the same identity, timestamp and
constraint-naming rules, which keeps Alembic autogenerate diffs stable.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit naming so Alembic autogenerate produces stable, reviewable diffs.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    """Base class for every ORM model in the platform."""

    metadata = metadata


class TimestampMixin:
    """``created_at`` / ``updated_at`` in UTC, maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, sort_order=100
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        sort_order=101,
    )


class IdMixin:
    """Surrogate ``bigint`` primary key.

    A generated identity column rather than a UUID: these tables are written in
    time order at high rate, where sequential keys keep index locality. Rows are
    referenced internally only - nothing user-facing exposes an id.
    """

    # sort_order keeps `id` first in generated DDL despite coming from a mixin.
    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, sort_order=-100
    )


class RecordMixin(IdMixin, TimestampMixin):
    """The usual combination: surrogate key plus audit timestamps."""
