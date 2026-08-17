"""Dashboard endpoint.

One aggregate query set behind one endpoint. The alternative -- exposing
Prometheus text and running Grafana -- is the industry-standard answer and is
discussed in the README; this trades standardisation for one fewer moving part
in compose and Kubernetes, and for full control over the panels.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.deps import SessionDep
from app.db.repositories.inference_logs import InferenceLogRepository
from app.db.repositories.metrics import MetricsRepository
from app.domain.enums import InferenceStatus

router = APIRouter(prefix="/metrics", tags=["metrics"])


class TimeBucketOut(BaseModel):
    bucket: datetime
    requests: int
    errors: int
    cancellations: int
    p50_latency_ms: int | None
    p95_latency_ms: int | None
    p50_ttft_ms: int | None


class ProviderBreakdownOut(BaseModel):
    provider: str
    model: str
    requests: int
    errors: int
    cancellations: int
    p50_latency_ms: int | None
    p95_latency_ms: int | None
    p95_ttft_ms: int | None
    input_tokens: int
    output_tokens: int


class TotalsOut(BaseModel):
    requests: int
    errors: int
    cancellations: int
    error_rate: float
    p50_latency_ms: int | None
    p95_latency_ms: int | None
    p95_ttft_ms: int | None
    input_tokens: int
    output_tokens: int


class IngestionHealthOut(BaseModel):
    """Whether the ingestion pipeline is alive, separate from whether chat is.

    These are different systems and they fail independently -- the chat API can
    be perfectly healthy while nothing at all is being logged.
    """

    last_ingested_at: datetime | None
    lag_seconds: float | None
    logs_total: int
    raw_events_total: int
    raw_events_pending: int
    raw_events_failed: int
    #: Derived server-side so every client agrees on what "stalled" means.
    is_stalled: bool


class MetricsSummary(BaseModel):
    window_minutes: int
    interval: str
    totals: TotalsOut
    series: list[TimeBucketOut]
    providers: list[ProviderBreakdownOut]
    ingestion: IngestionHealthOut


#: Beyond this, ingestion is treated as stalled rather than merely quiet. Well
#: above a normal end-to-end publish-to-write latency (milliseconds), low
#: enough to notice an outage inside a coffee break.
STALLED_AFTER_SECONDS = 120.0


def get_metrics_repo(session: SessionDep) -> MetricsRepository:
    return MetricsRepository(session)


MetricsRepoDep = Annotated[MetricsRepository, Depends(get_metrics_repo)]


@router.get("/summary", response_model=MetricsSummary)
async def summary(
    repo: MetricsRepoDep,
    window_minutes: Annotated[int, Query(ge=1, le=10_080)] = 60,
    interval: Literal["minute", "hour", "day"] = "minute",
) -> MetricsSummary:
    since = datetime.now(UTC) - timedelta(minutes=window_minutes)

    totals = await repo.totals(since=since)
    series = await repo.time_series(since=since, interval=interval)
    providers = await repo.by_provider(since=since)
    ingestion = await repo.ingestion_health()

    return MetricsSummary(
        window_minutes=window_minutes,
        interval=interval,
        totals=TotalsOut(
            requests=totals.requests,
            errors=totals.errors,
            cancellations=totals.cancellations,
            error_rate=round(totals.error_rate, 4),
            p50_latency_ms=totals.p50_latency_ms,
            p95_latency_ms=totals.p95_latency_ms,
            p95_ttft_ms=totals.p95_ttft_ms,
            input_tokens=totals.input_tokens,
            output_tokens=totals.output_tokens,
        ),
        # `asdict`, not `vars`: these dataclasses use `slots=True` and so
        # have no `__dict__` at all.
        series=[TimeBucketOut(**asdict(bucket)) for bucket in series],
        providers=[ProviderBreakdownOut(**asdict(row)) for row in providers],
        ingestion=IngestionHealthOut(
            last_ingested_at=ingestion.last_ingested_at,
            lag_seconds=ingestion.lag_seconds,
            logs_total=ingestion.logs_total,
            raw_events_total=ingestion.raw_events_total,
            raw_events_pending=ingestion.raw_events_pending,
            raw_events_failed=ingestion.raw_events_failed,
            # Nothing ever ingested is "not started", not "stalled" -- a fresh
            # deployment should not open on a red panel.
            is_stalled=(
                ingestion.lag_seconds is not None and ingestion.lag_seconds > STALLED_AFTER_SECONDS
            ),
        ),
    )


class RecentLogOut(BaseModel):
    id: str
    provider: str
    model: str
    status: str
    started_at: datetime
    latency_ms: int | None
    ttft_ms: int | None
    error_type: str | None
    error_message: str | None
    input_preview: str | None
    output_preview: str | None


@router.get("/errors", response_model=list[RecentLogOut])
async def recent_errors(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
) -> list[RecentLogOut]:
    """The failures themselves, not just their count.

    A rate tells you something broke; these rows tell you what. Served by the
    partial index on `status = 'error'`, so it stays cheap as the table grows.
    """
    logs = await InferenceLogRepository(session).list_recent(
        limit=limit, status=InferenceStatus.ERROR
    )

    return [
        RecentLogOut(
            id=str(log.id),
            provider=log.provider,
            model=log.model,
            status=log.status.value,
            started_at=log.started_at,
            latency_ms=log.latency_ms,
            ttft_ms=log.ttft_ms,
            error_type=log.error_type,
            error_message=log.error_message,
            input_preview=log.input_preview,
            output_preview=log.output_preview,
        )
        for log in logs
    ]
