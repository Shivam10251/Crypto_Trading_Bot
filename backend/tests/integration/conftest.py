"""PostgreSQL fixtures for data-model tests.

The model tests need a real PostgreSQL instance: NUMERIC precision, CHECK
constraints, JSONB and ON DELETE behaviour are exactly what is under test, and
SQLite would silently accept things PostgreSQL rejects.

When no database is reachable the tests skip with a clear reason rather than
failing, so `make test` stays useful on a machine without Docker.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import trading_bot.db.models  # noqa: F401  (registers tables)
from trading_bot.db.base import Base

# Captured at import time: the autouse fixture in the parent conftest strips
# TB_* variables during tests, and these are needed to reach the server.
_HOST = os.environ.get("TB_DATABASE__HOST", "127.0.0.1")
_PORT = os.environ.get("TB_DATABASE__PORT", "5432")
_USER = os.environ.get("TB_DATABASE__USER", "trading_bot")
# Falls back to the .env.example default, which is what docker-compose starts with.
_PASSWORD = os.environ.get("TB_DATABASE__PASSWORD", "change_me_locally")

TEST_DATABASE = "trading_bot_test"
_ADMIN_URL = f"postgresql+asyncpg://{_USER}:{_PASSWORD}@{_HOST}:{_PORT}/postgres"
TEST_URL = f"postgresql+asyncpg://{_USER}:{_PASSWORD}@{_HOST}:{_PORT}/{TEST_DATABASE}"


async def _provision() -> str | None:
    """Create the test database and schema. Returns a skip reason on failure."""
    admin = create_async_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            from sqlalchemy import text

            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DATABASE},
            )
            if not exists:
                await connection.execute(text(f'CREATE DATABASE "{TEST_DATABASE}"'))
    except Exception as exc:  # no server, wrong password, no permission
        return f"PostgreSQL unavailable at {_HOST}:{_PORT} ({type(exc).__name__}: {exc})"
    finally:
        await admin.dispose()

    engine = create_async_engine(TEST_URL)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()
    return None


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Session-wide test database with the schema applied.

    Synchronous on purpose: it owns its own event loop, so the async
    per-test fixtures stay function-scoped and simple.
    """
    reason = asyncio.run(_provision())
    if reason:
        pytest.skip(reason, allow_module_level=True)
    yield TEST_URL


@pytest.fixture
async def db(postgres_url: str) -> AsyncIterator[AsyncSession]:
    """Session wrapped in a transaction that is always rolled back.

    Tests see a clean database without paying to rebuild the schema each time.
    """
    engine = create_async_engine(postgres_url)
    connection = await engine.connect()
    transaction = await connection.begin()
    factory = async_sessionmaker(bind=connection, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        # Roll the session back first: a test that tripped a constraint leaves
        # the transaction in a failed state, and closing it in that state warns.
        await session.rollback()
        await session.close()
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()
        await engine.dispose()
