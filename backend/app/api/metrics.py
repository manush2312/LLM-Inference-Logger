"""Dashboard endpoint.

One aggregate query set behind one endpoint. The alternative -- exposing
Prometheus text and running Grafana -- is the industry-standard answer and is
discussed in the README; this trades standardisation for one fewer moving part
in compose and Kubernetes, and for full control over the panels.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

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


class LogRowOut(BaseModel):
    """One inference, in full.

    Wider than `RecentLogOut` on purpose. That model backs the errors panel,
    where the question is "what broke"; this one backs a browser over every call,
    where the questions are about cost and latency attribution, so the token
    counts, the completion reason and whether the call streamed all matter.
    """

    id: str
    conversation_id: str | None
    #: The assistant message this call produced, when it produced one. Absent for
    #: errors and cancellations -- which is itself worth seeing.
    message_id: str | None
    provider: str
    model: str
    status: str
    streamed: bool
    started_at: datetime
    completed_at: datetime
    #: The third timestamp. `completed_at` is when the model finished;
    #: `ingested_at` is when the worker wrote the row, and the gap between them is
    #: this call's own ingestion lag. The dashboard reports that lag in aggregate;
    #: per row it shows whether *this* record was delayed.
    ingested_at: datetime
    latency_ms: int | None
    ttft_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    finish_reason: str | None
    error_type: str | None
    error_message: str | None
    input_preview: str | None
    output_preview: str | None
    #: Vendor-specific detail kept because it is worth having and not worth a
    #: column each. In practice: `provider_request_id`, which is the identifier a
    #: vendor's support will ask for, and `reasoning_tokens`, which is the only
    #: thing that explains a 3-word answer reporting 451 output tokens on a
    #: reasoning model. Both were being captured and thrown away at the API edge.
    raw_metadata: dict[str, Any]


class LogPage(BaseModel):
    items: list[LogRowOut]
    #: Feed both back as `before` and `before_id` for the next page. Null means
    #: this is the last page.
    next_before: datetime | None
    next_before_id: str | None


@router.get("/logs", response_model=LogPage)
async def recent_logs(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
    provider: Annotated[str | None, Query(max_length=64)] = None,
    status: InferenceStatus | None = None,
    before: datetime | None = None,
    before_id: uuid.UUID | None = None,
) -> LogPage:
    """Every inference, newest first -- not only the failures.

    The dashboard answered "how is the system behaving" in aggregate and "what
    broke" for errors, and had no answer at all for "show me that one call".
    For a system whose product *is* the log, the individual row is the thing, and
    it was reachable only by opening psql.

    Paginated by keyset rather than OFFSET: this table is designed to grow
    without bound, and offset pagination re-scans every skipped row, so page 200
    costs 200 pages of work. The cursor rides `(started_at DESC, id DESC)`.
    """
    repo = InferenceLogRepository(session)
    rows = await repo.list_recent(
        limit=limit,
        provider=provider,
        status=status,
        before=before,
        before_id=before_id,
    )

    items = [
        LogRowOut(
            id=str(log.id),
            conversation_id=str(log.conversation_id) if log.conversation_id else None,
            message_id=str(log.message_id) if log.message_id else None,
            provider=log.provider,
            model=log.model,
            status=log.status.value,
            streamed=log.streamed,
            started_at=log.started_at,
            completed_at=log.completed_at,
            ingested_at=log.ingested_at,
            latency_ms=log.latency_ms,
            ttft_ms=log.ttft_ms,
            input_tokens=log.input_tokens,
            output_tokens=log.output_tokens,
            finish_reason=log.finish_reason,
            error_type=log.error_type,
            error_message=log.error_message,
            input_preview=log.input_preview,
            output_preview=log.output_preview,
            raw_metadata=log.raw_metadata or {},
        )
        for log in rows
    ]

    # A cursor is only offered when the page came back full. Emitting one for a
    # short page would advertise a next page that is empty, and a client that
    # follows cursors until they run out would make one wasted request per poll.
    last = rows[-1] if len(rows) == limit else None
    return LogPage(
        items=items,
        next_before=last.started_at if last else None,
        next_before_id=str(last.id) if last else None,
    )
