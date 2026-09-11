"""Shared fixtures.

Tests never touch the developer's real database or .env: every fixture builds an
explicit ``Settings`` object, and the SQLite override keeps the database layer
exercisable without infrastructure.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from trading_bot.core.config import DatabaseConfig, Settings, get_settings
from trading_bot.db import session as session_module

SQLITE_MEMORY = "sqlite+aiosqlite:///:memory:"
# A closed port: connecting always fails, which is how we test the unhealthy path.
UNREACHABLE_POSTGRES = "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/missing"

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip TB_* variables and reset cached/global state around every test."""
    for key in list(os.environ):
        if key.startswith("TB_"):
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    session_module._engine = None
    session_module._session_factory = None
    yield
    get_settings.cache_clear()
    session_module._engine = None
    session_module._session_factory = None


@pytest.fixture
def sqlite_settings() -> Settings:
    """Development-profile settings backed by in-memory SQLite."""
    return Settings(database=DatabaseConfig(url_override=SQLITE_MEMORY))


@pytest.fixture
def unreachable_db_settings() -> Settings:
    """Settings whose database can never be reached."""
    return Settings(database=DatabaseConfig(url_override=UNREACHABLE_POSTGRES))


@pytest.fixture
async def client(sqlite_settings: Settings) -> AsyncIterator[AsyncClient]:
    """HTTP client running the app's real lifespan (engine init + dispose)."""
    async for c in _client_for(sqlite_settings):
        yield c


@pytest.fixture
async def unhealthy_client(unreachable_db_settings: Settings) -> AsyncIterator[AsyncClient]:
    async for c in _client_for(unreachable_db_settings):
        yield c


async def _client_for(settings: Settings) -> AsyncIterator[AsyncClient]:
    from trading_bot.main import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
