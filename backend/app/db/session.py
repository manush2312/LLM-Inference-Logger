"""Database engine lifecycle and session provisioning.

`Database` owns the connection pool. It is constructed once during application
startup and handed to consumers through dependency injection -- there is no
module-level engine, because a global connection pool is exactly what makes an
app impossible to test and awkward to shut down cleanly.
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

from app.core.config import AppEnv, Settings
from app.core.logging import get_logger

log = get_logger(__name__)


class Database:
    """Owns the async engine and hands out sessions."""

    def __init__(self, settings: Settings) -> None:
        self._engine: AsyncEngine = create_async_engine(
            settings.database_url,
            # Recycle below typical proxy/firewall idle timeouts so the pool
            # never hands out a connection the network has already dropped.
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=10,
            max_overflow=10,
            echo=settings.app_env == AppEnv.LOCAL and settings.log_level == "DEBUG",
        )
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,  # objects stay usable after commit
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session scoped to one unit of work.

        Commits on success, rolls back on any exception. Callers therefore
        never write transaction boilerplate, and a half-applied write cannot
        escape a failed handler.
        """
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def check(self) -> bool:
        """Readiness probe: can we actually round-trip a query?"""
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # probe reports, never raises
            log.warning("database_check_failed", error=str(exc))
            return False
        return True

    async def dispose(self) -> None:
        await self._engine.dispose()
        log.info("database_disposed")
