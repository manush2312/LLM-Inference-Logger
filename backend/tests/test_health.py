"""Health probe behaviour.

These run with no Postgres: the database is replaced through the dependency
graph, which is the point of routing every resource through `deps`.
"""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_database


class _StubDatabase:
    def __init__(self, *, reachable: bool) -> None:
        self._reachable = reachable

    async def check(self) -> bool:
        return self._reachable


async def _client_with_database(app: FastAPI, *, reachable: bool) -> AsyncClient:
    app.dependency_overrides[get_database] = lambda: _StubDatabase(reachable=reachable)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_liveness_is_independent_of_dependencies(client: AsyncClient) -> None:
    """Liveness must pass with no Postgres and no Redis running.

    If it did not, a database outage would restart every API pod in a loop and
    turn a recoverable dependency failure into a total one.
    """
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_is_ready_when_dependencies_are_reachable(app: FastAPI) -> None:
    async with await _client_with_database(app, reachable=True) as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}


async def test_readiness_returns_503_when_database_is_unreachable(app: FastAPI) -> None:
    """Must be a 503, not a 200 with a sad body -- orchestrators read the status."""
    async with await _client_with_database(app, reachable=False) as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "unreachable"
