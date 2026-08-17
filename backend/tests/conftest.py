"""Shared test fixtures.

The guiding rule: unit tests must run with no Postgres, no Redis and no API
keys. Anything that needs a live service is marked `integration` and excluded
from the default `make test` run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import EventBusBackend, Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    """Isolated settings: in-memory bus, no credentials, quiet logs."""
    return Settings(
        app_env="local",
        log_level="WARNING",
        event_bus_backend=EventBusBackend.MEMORY,
        default_provider="mock",
        anthropic_api_key=None,
        openai_api_key=None,
    )


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with LifespanManager(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """HTTP client wired straight to the ASGI app -- no socket, no port."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
