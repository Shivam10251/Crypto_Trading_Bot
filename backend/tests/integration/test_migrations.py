"""The migration must produce exactly the schema the models describe.

Without this test, a model change that nobody generated a migration for would
pass every other test (they build the schema from metadata) and then fail on a
real deployment. Here the migration is applied to a scratch database and the
result is compared against the model metadata.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import trading_bot.db.models  # noqa: F401  (registers tables)
from tests.integration.conftest import _HOST, _PASSWORD, _PORT, _USER
from trading_bot.db.base import Base

pytestmark = pytest.mark.requires_postgres

MIGRATION_DATABASE = "trading_bot_migration_check"
BACKEND_DIR = Path(__file__).resolve().parents[2]
ADMIN_URL = f"postgresql+asyncpg://{_USER}:{_PASSWORD}@{_HOST}:{_PORT}/postgres"
TARGET_URL = f"postgresql+asyncpg://{_USER}:{_PASSWORD}@{_HOST}:{_PORT}/{MIGRATION_DATABASE}"


async def _recreate_database() -> None:
    engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{MIGRATION_DATABASE}"'))
            await connection.execute(text(f'CREATE DATABASE "{MIGRATION_DATABASE}"'))
    finally:
        await engine.dispose()


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    """Run alembic in a subprocess so env.py resolves settings independently."""
    environment = {
        **os.environ,
        "TB_PROFILE": "development",
        "TB_DATABASE__HOST": _HOST,
        "TB_DATABASE__PORT": _PORT,
        "TB_DATABASE__USER": _USER,
        "TB_DATABASE__PASSWORD": _PASSWORD,
        "TB_DATABASE__NAME": MIGRATION_DATABASE,
    }
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, test-only
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def migrated_database(postgres_url: str) -> Iterator[str]:
    """A database built purely by running the migrations."""
    asyncio.run(_recreate_database())
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"
    yield TARGET_URL


class TestMigrationParity:
    async def test_migrated_schema_matches_the_models(self, migrated_database: str) -> None:
        """No pending model changes: autogenerate finds nothing to do."""
        engine = create_async_engine(migrated_database)

        def _diff(connection: Connection) -> list[object]:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "compare_server_default": True},
            )
            return compare_metadata(context, Base.metadata)

        try:
            async with engine.connect() as connection:
                differences = await connection.run_sync(_diff)
        finally:
            await engine.dispose()

        assert differences == [], f"models and migrations disagree: {differences}"

    async def test_every_table_is_created_by_the_migration(self, migrated_database: str) -> None:
        engine = create_async_engine(migrated_database)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
                tables = {row[0] for row in rows}
        finally:
            await engine.dispose()

        assert set(Base.metadata.tables) <= tables
        assert "alembic_version" in tables


class TestDowngrade:
    def test_downgrade_removes_everything_it_created(self, migrated_database: str) -> None:
        """A migration that cannot be undone is a one-way door."""
        downgrade = _run_alembic("downgrade", "base")
        assert downgrade.returncode == 0, downgrade.stderr

        async def _remaining() -> set[str]:
            engine = create_async_engine(TARGET_URL)
            try:
                async with engine.connect() as connection:
                    rows = await connection.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'public'"
                        )
                    )
                    return {row[0] for row in rows}
            finally:
                await engine.dispose()

        tables = asyncio.run(_remaining())
        # alembic_version is alembic's own bookkeeping and stays behind.
        assert tables == {"alembic_version"}

        # Leave the scratch database usable for any later run.
        assert _run_alembic("upgrade", "head").returncode == 0
