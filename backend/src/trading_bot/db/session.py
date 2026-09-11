"""Async engine and session management.

The engine is created once per process and shared. Callers never build their own
engine; they take an ``AsyncSession`` from ``session_scope`` (application code)
or the FastAPI dependency in ``trading_bot.api.deps``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from trading_bot.core.config import DatabaseConfig
from trading_bot.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _engine_kwargs(config: DatabaseConfig) -> dict[str, object]:
    # SQLite (used by tests) rejects the pooling arguments PostgreSQL needs.
    if config.dsn().startswith("sqlite"):
        return {"echo": config.echo_sql}
    return {
        "echo": config.echo_sql,
        "pool_size": config.pool_size,
        "max_overflow": config.max_overflow,
        "pool_timeout": config.pool_timeout_seconds,
        "pool_pre_ping": True,
    }


def init_engine(config: DatabaseConfig) -> AsyncEngine:
    """Create the process-wide engine. Idempotent."""
    global _engine, _session_factory
    if _engine is not None:
        return _engine

    _engine = create_async_engine(config.dsn(), **_engine_kwargs(config))
    _session_factory = async_sessionmaker(
        _engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    logger.info("database.engine_initialised", dsn=config.safe_dsn())
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("database engine not initialised; call init_engine() first")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("database engine not initialised; call init_engine() first")
    return _session_factory


async def dispose_engine() -> None:
    """Close all pooled connections. Called on application shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("database.engine_disposed")
    _engine = None
    _session_factory = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commits on success, rolls back on any exception."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def check_connection() -> bool:
    """Cheap liveness probe used by the health endpoint. Never raises."""
    if _engine is None:
        return False
    try:
        async with _engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # health checks must never propagate
        logger.warning("database.health_check_failed", error=str(exc))
        return False
