"""Liveness and readiness endpoints.

Two distinct probes, because Kubernetes asks two distinct questions:

* `/healthz` -- "is this process alive?" A failure here gets the pod killed, so
  it must never depend on downstream services. A Postgres outage restarting
  every API pod in a loop is a self-inflicted outage.
* `/readyz`  -- "can this process serve traffic right now?" A failure here only
  removes the pod from the load-balancer pool, which is the correct response
  to a dependency being unreachable.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, str]


@router.get("/healthz", response_model=LivenessResponse)
async def liveness() -> LivenessResponse:
    return LivenessResponse()


@router.get("/readyz", response_model=ReadinessResponse)
async def readiness() -> ReadinessResponse:
    """Dependency checks are registered here as later phases add them."""
    checks: dict[str, str] = {}
    healthy = all(result == "ok" for result in checks.values())
    return ReadinessResponse(status="ready" if healthy else "degraded", checks=checks)
