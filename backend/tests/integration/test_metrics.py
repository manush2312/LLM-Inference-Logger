"""Dashboard aggregates.

The first test here is the important one. With `started_at`, `completed_at`
and `ingested_at` adjacent in one table, grouping by the wrong column is a
one-word mistake that review will not catch and that produces a plausible,
entirely wrong chart.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text

from app.core.config import EventBusBackend, Settings, get_settings
from app.db.repositories.inference_logs import InferenceLogRepository
from app.db.repositories.metrics import MetricsRepository
from app.db.session import Database
from app.domain.enums import InferenceStatus
from app.domain.events import InferenceEvent
from app.main import create_app

pytestmark = pytest.mark.integration


def make_event(*, completed_at: datetime, **overrides: Any) -> InferenceEvent:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "provider": "mock",
        "model": "mock",
        "status": InferenceStatus.SUCCESS,
        "started_at": completed_at - timedelta(milliseconds=200),
        "completed_at": completed_at,
        "latency_ms": 200,
        "input_tokens": 10,
        "output_tokens": 40,
    }
    return InferenceEvent(**(defaults | overrides))


class _ApiClient:
    """An HTTP client that also hands back the database it is talking to.

    Lets an endpoint test seed rows directly and then assert on the serialised
    response, without a second fixture to keep in sync.
    """

    def __init__(self, client: AsyncClient, database: Database) -> None:
        self._client = client
        self.database = database

    async def get(self, path: str) -> Response:
        return await self._client.get(path)


@pytest.fixture
async def api_client(integration_settings: Settings) -> AsyncIterator[_ApiClient]:
    get_settings.cache_clear()
    app = create_app(
        integration_settings.model_copy(
            update={
                "log_level": "WARNING",
                "event_bus_backend": EventBusBackend.MEMORY,
                "ollama_enabled": False,
            }
        )
    )
    async with LifespanManager(app):
        async with app.state.database.session() as session:
            await session.execute(
                text(
                    "TRUNCATE conversations, messages, inference_logs, events_raw "
                    "RESTART IDENTITY CASCADE"
                )
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield _ApiClient(client, app.state.database)


@pytest.fixture
async def clean(database: Database) -> Database:
    async with database.session() as session:
        await session.execute(
            text(
                "TRUNCATE conversations, messages, inference_logs, events_raw "
                "RESTART IDENTITY CASCADE"
            )
        )
    return database


async def insert(database: Database, *events: InferenceEvent) -> None:
    async with database.session() as session:
        repo = InferenceLogRepository(session)
        for event in events:
            await repo.upsert(event)


async def force_ingested_at(database: Database, when: datetime) -> None:
    """Rewrite `ingested_at`, which is otherwise a server-side default.

    Simulates a worker that fell behind and then caught up in a burst.
    """
    async with database.session() as session:
        await session.execute(text("UPDATE inference_logs SET ingested_at = :when"), {"when": when})


# --- The bucketing column --------------------------------------------------


async def test_buckets_follow_completed_at_not_ingested_at(clean: Database) -> None:
    """A worker backlog must not render as a traffic spike.

    Two calls that happened an hour apart are ingested in the same instant --
    exactly what a recovering worker does. Bucketing on `ingested_at` would
    collapse them into one bucket and invent a burst of traffic that never
    occurred; bucketing on `completed_at` keeps them where they actually
    happened.
    """
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    long_ago = now - timedelta(hours=1)

    await insert(
        clean,
        make_event(completed_at=long_ago),
        make_event(completed_at=now),
    )
    # Both rows land in the database at the same moment, an hour after the
    # first call actually completed.
    await force_ingested_at(clean, now)

    async with clean.session() as session:
        series = await MetricsRepository(session).time_series(
            since=now - timedelta(hours=2), interval="hour"
        )

    assert len(series) == 2, "grouped by ingest time, not by when the calls happened"
    assert [bucket.requests for bucket in series] == [1, 1]


async def test_window_filter_also_uses_completed_at(clean: Database) -> None:
    """The `since` filter must agree with the bucketing column.

    If they disagreed, a late-ingested row could be counted in the totals but
    fall outside every bucket -- totals and chart silently disagreeing.
    """
    now = datetime.now(UTC)
    await insert(clean, make_event(completed_at=now - timedelta(hours=3)))
    await force_ingested_at(clean, now)

    async with clean.session() as session:
        totals = await MetricsRepository(session).totals(since=now - timedelta(minutes=30))

    assert totals.requests == 0, "an old call was counted because it was ingested recently"


# --- Aggregates ------------------------------------------------------------


async def test_totals_separate_errors_from_cancellations(clean: Database) -> None:
    """Conflating them would inflate the error rate with closed browser tabs."""
    now = datetime.now(UTC)
    await insert(
        clean,
        make_event(completed_at=now),
        make_event(completed_at=now, status=InferenceStatus.ERROR, error_message="429"),
        make_event(completed_at=now, status=InferenceStatus.CANCELLED),
    )

    async with clean.session() as session:
        totals = await MetricsRepository(session).totals(since=now - timedelta(minutes=5))

    assert totals.requests == 3
    assert totals.errors == 1
    assert totals.cancellations == 1
    assert totals.error_rate == pytest.approx(1 / 3)


async def test_percentiles_ignore_rows_with_no_measurement(clean: Database) -> None:
    """A call that failed before its first token has no TTFT.

    Treating that null as zero would drag every percentile down and make the
    system look faster than it is.
    """
    now = datetime.now(UTC)
    await insert(
        clean,
        make_event(completed_at=now, latency_ms=100, ttft_ms=100),
        make_event(completed_at=now, latency_ms=300, ttft_ms=300),
        make_event(
            completed_at=now,
            status=InferenceStatus.ERROR,
            error_message="failed instantly",
            latency_ms=None,
            ttft_ms=None,
        ),
    )

    async with clean.session() as session:
        totals = await MetricsRepository(session).totals(since=now - timedelta(minutes=5))

    assert totals.p50_latency_ms == 200, "nulls were counted as zero"


async def test_breakdown_groups_by_provider_and_model(clean: Database) -> None:
    now = datetime.now(UTC)
    await insert(
        clean,
        make_event(completed_at=now, provider="mock", model="mock"),
        make_event(completed_at=now, provider="mock", model="mock"),
        make_event(completed_at=now, provider="anthropic", model="claude-opus-5"),
    )

    async with clean.session() as session:
        rows = await MetricsRepository(session).by_provider(since=now - timedelta(minutes=5))

    assert [(r.provider, r.model, r.requests) for r in rows] == [
        ("mock", "mock", 2),
        ("anthropic", "claude-opus-5", 1),
    ]


async def test_empty_window_returns_zeros_not_nulls(clean: Database) -> None:
    """A quiet period is zero traffic, not a broken response."""
    async with clean.session() as session:
        totals = await MetricsRepository(session).totals(since=datetime.now(UTC))

    assert totals.requests == 0
    assert totals.error_rate == 0.0
    assert totals.input_tokens == 0


# --- Ingestion health ------------------------------------------------------


async def test_ingestion_lag_reflects_the_last_successful_write(clean: Database) -> None:
    """The panel that would have caught the silent NOGROUP outage.

    The worker there was alive and passing its liveness probe while ingesting
    nothing. Lag is time since the last write, so it grows regardless of *why*
    ingestion stopped.
    """
    now = datetime.now(UTC)
    await insert(clean, make_event(completed_at=now))
    await force_ingested_at(clean, now - timedelta(minutes=10))

    async with clean.session() as session:
        health = await MetricsRepository(session).ingestion_health()

    assert health.lag_seconds is not None
    assert health.lag_seconds > 500, "a ten-minute stall was not visible"
    assert health.logs_total == 1


async def test_ingestion_health_is_not_limited_to_the_query_window(clean: Database) -> None:
    """Deliberately unwindowed.

    A windowed version would report "no data" during exactly the outage it
    exists to reveal -- indistinguishable from a quiet period.
    """
    now = datetime.now(UTC)
    await insert(clean, make_event(completed_at=now - timedelta(days=2)))
    await force_ingested_at(clean, now - timedelta(days=2))

    async with clean.session() as session:
        health = await MetricsRepository(session).ingestion_health()

    assert health.logs_total == 1
    assert health.lag_seconds is not None and health.lag_seconds > 86_400


async def test_no_ingestion_yet_is_not_reported_as_a_stall(clean: Database) -> None:
    """A fresh deployment must not open on a red panel."""
    async with clean.session() as session:
        health = await MetricsRepository(session).ingestion_health()

    assert health.last_ingested_at is None
    assert health.lag_seconds is None
    assert health.logs_total == 0


# --- The endpoint, not just the repository ---------------------------------
#
# These exist because the repository tests all passed while `/metrics/summary`
# returned a 500 in the container: the handler used `vars()` on dataclasses
# declared with `slots=True`, which have no `__dict__`. Testing the query layer
# is not testing the endpoint, and the serialisation boundary between them is
# exactly where that class of mistake lives.


async def test_summary_endpoint_serialises_every_section(api_client: _ApiClient) -> None:
    response = await api_client.get("/metrics/summary?window_minutes=60")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"window_minutes", "interval", "totals", "series", "providers", "ingestion"}
    assert "error_rate" in body["totals"]
    assert "is_stalled" in body["ingestion"]


async def test_summary_endpoint_returns_populated_series(api_client: _ApiClient) -> None:
    """Exercises the list-of-dataclasses path that the 500 came from."""
    now = datetime.now(UTC)
    async with api_client.database.session() as session:
        repo = InferenceLogRepository(session)
        await repo.upsert(make_event(completed_at=now))
        await repo.upsert(
            make_event(completed_at=now, status=InferenceStatus.ERROR, error_message="429")
        )

    body = (await api_client.get("/metrics/summary?window_minutes=60")).json()

    assert body["totals"]["requests"] == 2
    assert len(body["series"]) >= 1
    assert body["series"][0]["requests"] >= 1
    assert len(body["providers"]) >= 1


async def test_errors_endpoint_lists_failures(api_client: _ApiClient) -> None:
    now = datetime.now(UTC)
    async with api_client.database.session() as session:
        await InferenceLogRepository(session).upsert(
            make_event(
                completed_at=now,
                status=InferenceStatus.ERROR,
                error_type="RateLimitError",
                error_message="429 Too Many Requests",
            )
        )

    rows = (await api_client.get("/metrics/errors")).json()

    assert len(rows) == 1
    assert rows[0]["error_type"] == "RateLimitError"


async def test_invalid_interval_is_rejected_by_validation(api_client: _ApiClient) -> None:
    """`date_trunc` takes a string; an unvalidated one would be injection."""
    response = await api_client.get("/metrics/summary?interval=minute'); DROP TABLE--")

    assert response.status_code == 422


async def test_breakdown_reports_cancellations_per_provider(clean: Database) -> None:
    """Aggregated cancellations hide which provider is being abandoned.

    A provider cancelled disproportionately often is usually one that is slow to
    first token, and that is a fact about *it* -- invisible if cancellations only
    ever appear in the totals.
    """
    now = datetime.now(UTC)
    await insert(
        clean,
        make_event(completed_at=now, provider="slowvendor", model="m"),
        make_event(
            completed_at=now, provider="slowvendor", model="m", status=InferenceStatus.CANCELLED
        ),
        make_event(completed_at=now, provider="fastvendor", model="m"),
    )

    async with clean.session() as session:
        rows = {
            r.provider: r
            for r in await MetricsRepository(session).by_provider(since=now - timedelta(minutes=5))
        }

    assert rows["slowvendor"].cancellations == 1
    assert rows["fastvendor"].cancellations == 0
