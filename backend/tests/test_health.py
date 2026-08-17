from __future__ import annotations

from httpx import AsyncClient


async def test_liveness_is_independent_of_dependencies(client: AsyncClient) -> None:
    """Liveness must pass with no Postgres and no Redis running."""
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_reports_check_results(client: AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ready", "degraded"}
    assert isinstance(body["checks"], dict)
