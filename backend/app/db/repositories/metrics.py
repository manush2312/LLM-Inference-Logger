"""Dashboard aggregates.

Every time-series query buckets on **`completed_at`**, never `ingested_at`.
With three timestamps sitting adjacent in one table that is an easy thing to
get wrong and an almost impossible thing to notice: `ingested_at` is when the
worker wrote the row, so bucketing on it would attribute a batch of catch-up
writes to the minute the worker recovered. A twenty-minute outage would render
as twenty silent minutes followed by a traffic spike that never happened --
and every latency percentile would be computed over the wrong population.

`test_buckets_follow_completed_at_not_ingested_at` pins this, because code
review alone will not catch a one-word change here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Float, Integer, case, cast, func, select
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.functions import percentile_cont

from app.db.models import InferenceLog, RawEvent
from app.db.repositories.base import Repository
from app.domain.enums import InferenceStatus, ProcessingStatus

#: The column every dashboard query groups by. Named once so the choice is a
#: single edit rather than a value repeated across queries.
BUCKET_COLUMN = InferenceLog.completed_at


@dataclass(frozen=True, slots=True)
class TimeBucket:
    bucket: datetime
    requests: int
    errors: int
    cancellations: int
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    p50_ttft_ms: float | None


@dataclass(frozen=True, slots=True)
class ProviderBreakdown:
    provider: str
    model: str
    requests: int
    errors: int
    #: Reported per provider, not only in the totals. A provider being
    #: cancelled disproportionately often is a signal about *it* -- usually
    #: that it is too slow to first token and users give up -- and that is
    #: invisible if cancellations are only ever aggregated.
    cancellations: int
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    #: Per provider, because time-to-first-token is the number that differs most
    #: between them -- a thinking model can spend tens of seconds before its
    #: first visible token while a fast one answers in under a second.
    p95_ttft_ms: float | None
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class Totals:
    requests: int
    errors: int
    cancellations: int
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    p95_ttft_ms: float | None
    input_tokens: int
    output_tokens: int

    @property
    def error_rate(self) -> float:
        return (self.errors / self.requests) if self.requests else 0.0


@dataclass(frozen=True, slots=True)
class IngestionHealth:
    """Whether the pipeline is actually working.

    Exists because of a real outage: deleting a Redis stream also deletes its
    consumer group, and the worker then looped on NOGROUP -- alive, answering
    its liveness probe, ingesting nothing. Nothing crashed, so nothing alerted.

    `lag_seconds` is the direct read on that: it is time since the last
    successful write, so it grows without bound whenever ingestion has stopped
    for *any* reason -- a dead worker, an unreachable database, a consumer
    group that no longer exists. A panel that only showed chat traffic would
    have looked perfectly healthy throughout.
    """

    last_ingested_at: datetime | None
    lag_seconds: float | None
    logs_total: int
    raw_events_total: int
    raw_events_pending: int
    raw_events_failed: int


class MetricsRepository(Repository):
    async def totals(self, *, since: datetime) -> Totals:
        stmt = select(
            func.count().label("requests"),
            _count_where(InferenceLog.status == InferenceStatus.ERROR).label("errors"),
            _count_where(InferenceLog.status == InferenceStatus.CANCELLED).label("cancels"),
            _percentile(0.5, InferenceLog.latency_ms).label("p50"),
            _percentile(0.95, InferenceLog.latency_ms).label("p95"),
            _percentile(0.95, InferenceLog.ttft_ms).label("p95_ttft"),
            func.coalesce(func.sum(InferenceLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(InferenceLog.output_tokens), 0).label("output_tokens"),
        ).where(since <= BUCKET_COLUMN)

        row = (await self._session.execute(stmt)).one()
        return Totals(
            requests=row.requests,
            errors=row.errors,
            cancellations=row.cancels,
            p50_latency_ms=row.p50,
            p95_latency_ms=row.p95,
            p95_ttft_ms=row.p95_ttft,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
        )

    async def time_series(self, *, since: datetime, interval: str = "minute") -> list[TimeBucket]:
        """Throughput, errors and latency percentiles per bucket.

        `interval` is validated against a fixed set rather than interpolated:
        `date_trunc` takes a string, and an unvalidated one reaching it would
        be SQL injection through the back door.
        """
        if interval not in _ALLOWED_INTERVALS:
            raise ValueError(f"unsupported interval: {interval!r}")

        bucket = func.date_trunc(interval, BUCKET_COLUMN).label("bucket")

        stmt = (
            select(
                bucket,
                func.count().label("requests"),
                _count_where(InferenceLog.status == InferenceStatus.ERROR).label("errors"),
                _count_where(InferenceLog.status == InferenceStatus.CANCELLED).label("cancels"),
                _percentile(0.5, InferenceLog.latency_ms).label("p50"),
                _percentile(0.95, InferenceLog.latency_ms).label("p95"),
                _percentile(0.5, InferenceLog.ttft_ms).label("p50_ttft"),
            )
            .where(since <= BUCKET_COLUMN)
            .group_by(bucket)
            .order_by(bucket)
        )

        return [
            TimeBucket(
                bucket=row.bucket,
                requests=row.requests,
                errors=row.errors,
                cancellations=row.cancels,
                p50_latency_ms=row.p50,
                p95_latency_ms=row.p95,
                p50_ttft_ms=row.p50_ttft,
            )
            for row in (await self._session.execute(stmt)).all()
        ]

    async def by_provider(self, *, since: datetime) -> list[ProviderBreakdown]:
        stmt = (
            select(
                InferenceLog.provider,
                InferenceLog.model,
                func.count().label("requests"),
                _count_where(InferenceLog.status == InferenceStatus.ERROR).label("errors"),
                _count_where(InferenceLog.status == InferenceStatus.CANCELLED).label("cancels"),
                _percentile(0.5, InferenceLog.latency_ms).label("p50"),
                _percentile(0.95, InferenceLog.latency_ms).label("p95"),
                _percentile(0.95, InferenceLog.ttft_ms).label("p95_ttft"),
                func.coalesce(func.sum(InferenceLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(InferenceLog.output_tokens), 0).label("output_tokens"),
            )
            .where(since <= BUCKET_COLUMN)
            .group_by(InferenceLog.provider, InferenceLog.model)
            .order_by(func.count().desc())
        )

        return [
            ProviderBreakdown(
                provider=row.provider,
                model=row.model,
                requests=row.requests,
                errors=row.errors,
                cancellations=row.cancels,
                p50_latency_ms=row.p50,
                p95_latency_ms=row.p95,
                p95_ttft_ms=row.p95_ttft,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
            )
            for row in (await self._session.execute(stmt)).all()
        ]

    async def ingestion_health(self) -> IngestionHealth:
        """Is the pipeline moving? Deliberately unfiltered by time window.

        A time-windowed version would report zero rows during exactly the
        outage it is meant to reveal, which is the failure this panel exists to
        make visible.
        """
        log_stmt = select(
            func.max(InferenceLog.ingested_at).label("last_ingested_at"),
            func.count().label("logs_total"),
        )
        log_row = (await self._session.execute(log_stmt)).one()

        raw_stmt = select(
            func.count().label("total"),
            _count_where(RawEvent.processing_status == ProcessingStatus.PENDING).label("pending"),
            _count_where(RawEvent.processing_status == ProcessingStatus.FAILED).label("failed"),
        )
        raw_row = (await self._session.execute(raw_stmt)).one()

        # Computed in Postgres so the answer does not depend on the API pod's
        # clock agreeing with the database's.
        lag = None
        if log_row.last_ingested_at is not None:
            lag = await self._session.scalar(
                select(
                    cast(
                        func.extract("epoch", func.now() - func.max(InferenceLog.ingested_at)),
                        Float,
                    )
                )
            )

        return IngestionHealth(
            last_ingested_at=log_row.last_ingested_at,
            lag_seconds=float(lag) if lag is not None else None,
            logs_total=log_row.logs_total,
            raw_events_total=raw_row.total,
            raw_events_pending=raw_row.pending,
            raw_events_failed=raw_row.failed,
        )


_ALLOWED_INTERVALS = frozenset({"second", "minute", "hour", "day"})


def _count_where(condition: ColumnElement[bool]) -> ColumnElement[int]:
    """COUNT of rows matching a condition, as a column expression."""
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def _percentile(
    fraction: float, column: InstrumentedAttribute[Any] | ColumnElement[Any]
) -> ColumnElement[int]:
    """A percentile that ignores NULLs.

    `latency_ms` and `ttft_ms` are legitimately null -- a call that failed
    before its first token has no TTFT. `percentile_cont` skips nulls, so the
    percentile describes calls that actually produced a measurement rather
    than silently treating "no data" as zero.
    """
    return cast(percentile_cont(fraction).within_group(column), Integer)
