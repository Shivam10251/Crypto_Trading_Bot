"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.core.config import Settings, get_settings
from trading_bot.db.session import get_session_factory


async def db_session() -> AsyncIterator[AsyncSession]:
    """Request-scoped session. Rolls back if the handler raises."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(db_session)]
