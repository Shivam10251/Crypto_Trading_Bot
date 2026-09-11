"""Engine lifecycle, transactional scope and the health probe."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from trading_bot.core.config import DatabaseConfig
from trading_bot.db.base import NAMING_CONVENTION, Base
from trading_bot.db.session import (
    check_connection,
    dispose_engine,
    get_engine,
    get_session_factory,
    init_engine,
    session_scope,
)

SQLITE = DatabaseConfig(url_override="sqlite+aiosqlite:///:memory:")
UNREACHABLE = DatabaseConfig(url_override="postgresql+asyncpg://n:n@127.0.0.1:1/missing")


class TestEngineLifecycle:
    async def test_init_is_idempotent(self) -> None:
        first = init_engine(SQLITE)
        assert init_engine(SQLITE) is first
        await dispose_engine()

    async def test_accessors_raise_before_init(self) -> None:
        with pytest.raises(RuntimeError, match="not initialised"):
            get_engine()
        with pytest.raises(RuntimeError, match="not initialised"):
            get_session_factory()

    async def test_dispose_resets_state(self) -> None:
        init_engine(SQLITE)
        await dispose_engine()
        with pytest.raises(RuntimeError, match="not initialised"):
            get_engine()

    async def test_sqlite_skips_pool_arguments(self) -> None:
        # Would raise TypeError if pool_size were passed to the SQLite dialect.
        engine = init_engine(SQLITE)
        assert engine.url.get_backend_name() == "sqlite"
        await dispose_engine()


class TestSessionScope:
    async def test_commits_on_success(self) -> None:
        init_engine(SQLITE)
        async with session_scope() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
        await dispose_engine()

    async def test_rolls_back_and_re_raises_on_error(self) -> None:
        init_engine(SQLITE)
        with pytest.raises(ValueError, match="strategy exploded"):
            async with session_scope() as session:
                await session.execute(text("SELECT 1"))
                raise ValueError("strategy exploded")
        await dispose_engine()

    async def test_requires_initialised_engine(self) -> None:
        with pytest.raises(RuntimeError, match="not initialised"):
            async with session_scope():
                pass


class TestHealthProbe:
    async def test_false_when_engine_missing(self) -> None:
        assert await check_connection() is False

    async def test_true_when_reachable(self) -> None:
        init_engine(SQLITE)
        assert await check_connection() is True
        await dispose_engine()

    async def test_false_when_unreachable_and_never_raises(self) -> None:
        init_engine(UNREACHABLE)
        assert await check_connection() is False
        await dispose_engine()

    async def test_unreachable_database_still_raises_for_real_queries(self) -> None:
        """The probe swallows errors; actual query paths must not."""
        init_engine(UNREACHABLE)
        with pytest.raises((OperationalError, OSError)):
            async with session_scope() as session:
                await session.execute(text("SELECT 1"))
        await dispose_engine()


class TestConventions:
    def test_naming_convention_is_applied(self) -> None:
        assert Base.metadata.naming_convention == NAMING_CONVENTION

    def test_models_are_registered(self) -> None:
        """Importing the models package must populate the metadata."""
        import trading_bot.db.models  # noqa: F401

        assert "opportunities" in Base.metadata.tables
        assert len(Base.metadata.tables) == 13
