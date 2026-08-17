"""Fixtures for tests that need a real Postgres.

Everything in this package is marked `integration` and excluded from the
default `make test` run. Run `make infra-up && make migrate` first.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import Database

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def integration_settings() -> Settings:
    """Real settings from .env -- these tests talk to the real services."""
    return get_settings()


@pytest.fixture
async def database(integration_settings: Settings) -> AsyncIterator[Database]:
    db = Database(integration_settings)
    yield db
    await db.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    """A clean database per test.

    TRUNCATE ... CASCADE rather than dropping and recreating the schema: it is
    far faster, and it exercises the same migrated schema the application runs
    against instead of one built from `metadata.create_all` that may have
    quietly diverged from the migrations.
    """
    async with database.session() as s:
        await s.execute(
            text(
                "TRUNCATE conversations, messages, inference_logs, events_raw "
                "RESTART IDENTITY CASCADE"
            )
        )

    async with database.session() as s:
        yield s
