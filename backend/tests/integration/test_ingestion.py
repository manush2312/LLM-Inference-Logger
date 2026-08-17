"""Ingestion worker against a real database.

Covers the properties that keep the log trustworthy: redelivery does not
duplicate, malformed events are quarantined rather than dropped or fatal, and
nothing is acknowledged before it is durably written.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.db.session import Database
from app.domain.enums import InferenceStatus, ProcessingStatus
from app.domain.events import InferenceEvent
from app.events.bus import DeliveredEvent, EventStream, InMemoryEventBus
from app.worker.consumer import IngestionWorker

pytestmark = pytest.mark.integration


def make_event(**overrides: Any) -> InferenceEvent:
    started = datetime.now(UTC)
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "provider": "mock",
        "model": "mock",
        "status": InferenceStatus.SUCCESS,
        "started_at": started,
        "completed_at": started + timedelta(milliseconds=90),
        "latency_ms": 90,
        "input_tokens": 5,
        "output_tokens": 25,
    }
    return InferenceEvent(**(defaults | overrides))


class _ScriptedStream(InMemoryEventBus):
    """Lets a test inject arbitrary payloads, including invalid ones."""

    def offer(self, payload: dict[str, Any]) -> None:
        self._counter += 1
        self._pending.append(DeliveredEvent(delivery_id=str(self._counter), payload=payload))


async def drain(stream: EventStream, database: Database) -> Any:
    worker = IngestionWorker(stream=stream, database=database, block_ms=0)
    return await worker.process_batch()


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


async def count(database: Database, table: str) -> int:
    async with database.session() as session:
        result = await session.scalar(text(f"SELECT count(*) FROM {table}"))
    return int(result or 0)


# --- The happy path --------------------------------------------------------


async def test_an_event_becomes_a_log_row_and_a_raw_row(clean: Database) -> None:
    stream = _ScriptedStream()
    event = make_event()
    await stream.publish(event)

    outcome = await drain(stream, clean)

    assert outcome.inserted == 1
    assert await count(clean, "inference_logs") == 1
    assert await count(clean, "events_raw") == 1

    async with clean.session() as session:
        row = (
            await session.execute(text("SELECT provider, status, latency_ms FROM inference_logs"))
        ).one()
    assert row.provider == "mock"
    assert row.status == "success"
    assert row.latency_ms == 90


async def test_raw_event_is_marked_processed(clean: Database) -> None:
    stream = _ScriptedStream()
    await stream.publish(make_event())

    await drain(stream, clean)

    async with clean.session() as session:
        status = await session.scalar(text("SELECT processing_status FROM events_raw"))
    assert status == ProcessingStatus.PROCESSED.value


# --- At-least-once delivery must not mean at-least-once rows ---------------


async def test_redelivery_does_not_duplicate_the_row(clean: Database) -> None:
    """The property that keeps every dashboard count honest across restarts."""
    stream = _ScriptedStream()
    event = make_event()

    await stream.publish(event)
    first = await drain(stream, clean)

    # Same event, delivered again -- a crash between write and ack.
    await stream.publish(event)
    second = await drain(stream, clean)

    assert first.inserted == 1
    assert second.inserted == 0
    assert second.duplicates == 1
    assert await count(clean, "inference_logs") == 1


# --- Bad events are quarantined, not dropped and not fatal -----------------


async def test_schema_violation_is_dead_lettered_and_retained(clean: Database) -> None:
    """A rejected event must still be recoverable once the bug is fixed."""
    stream = _ScriptedStream()
    stream.offer(
        {
            "id": str(uuid.uuid4()),
            "event_type": "inference.completed",
            "provider": "mock",
            # `model`, `status` and both timestamps are missing.
        }
    )

    outcome = await drain(stream, clean)

    assert outcome.rejected == 1
    assert await count(clean, "inference_logs") == 0
    # The payload survives, with the reason recorded, so it can be replayed.
    assert await count(clean, "events_raw") == 1
    assert stream.dead_lettered and stream.dead_lettered[0][1] == "schema_validation_failed"

    async with clean.session() as session:
        row = (
            await session.execute(
                text("SELECT processing_status, processing_error FROM events_raw")
            )
        ).one()
    assert row.processing_status == ProcessingStatus.FAILED.value
    assert row.processing_error


async def test_event_without_a_usable_id_is_rejected_before_the_database(
    clean: Database,
) -> None:
    """No id means no idempotency, so it cannot safely enter the pipeline."""
    stream = _ScriptedStream()
    stream.offer({"id": "not-a-uuid", "provider": "mock"})

    outcome = await drain(stream, clean)

    assert outcome.rejected == 1
    assert await count(clean, "events_raw") == 0
    assert stream.dead_lettered[0][1] == "missing_or_invalid_id"


async def test_one_bad_event_does_not_block_the_good_ones(clean: Database) -> None:
    """A malformed payload must not stop the pipeline behind it."""
    stream = _ScriptedStream()
    await stream.publish(make_event())
    stream.offer({"id": str(uuid.uuid4()), "provider": "mock"})  # invalid
    await stream.publish(make_event())

    outcome = await drain(stream, clean)

    assert outcome.received == 3
    assert outcome.inserted == 2
    assert outcome.rejected == 1


# --- Error and cancellation rows survive the round trip --------------------


@pytest.mark.parametrize(
    ("status", "extra"),
    [
        (InferenceStatus.ERROR, {"error_type": "RateLimitError", "error_message": "429"}),
        (InferenceStatus.CANCELLED, {"error_message": "Cancelled by client"}),
    ],
)
async def test_non_success_outcomes_are_persisted(
    clean: Database, status: InferenceStatus, extra: dict[str, Any]
) -> None:
    stream = _ScriptedStream()
    await stream.publish(make_event(status=status, **extra))

    await drain(stream, clean)

    async with clean.session() as session:
        stored = await session.scalar(text("SELECT status FROM inference_logs"))
    assert stored == status.value


async def test_an_empty_poll_is_not_an_error(clean: Database) -> None:
    outcome = await drain(_ScriptedStream(), clean)

    assert outcome.received == 0
    assert outcome.inserted == 0
