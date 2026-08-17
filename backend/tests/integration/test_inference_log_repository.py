from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.conversations import ConversationRepository
from app.db.repositories.inference_logs import InferenceLogRepository
from app.domain.enums import InferenceStatus
from app.domain.events import InferenceEvent

pytestmark = pytest.mark.integration


def make_event(**overrides: object) -> InferenceEvent:
    started = datetime.now(UTC)
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "provider": "mock",
        "model": "mock-1",
        "status": InferenceStatus.SUCCESS,
        "started_at": started,
        "completed_at": started + timedelta(milliseconds=120),
        "latency_ms": 120,
        "input_tokens": 10,
        "output_tokens": 20,
    }
    return InferenceEvent(**(defaults | overrides))  # type: ignore[arg-type]


async def test_redelivery_of_the_same_event_does_not_duplicate_the_row(
    session: AsyncSession,
) -> None:
    """At-least-once delivery must not become at-least-once *rows*.

    This is the single property that keeps every count and percentile on the
    dashboard honest after a worker restart.
    """
    repo = InferenceLogRepository(session)
    event = make_event()

    assert await repo.upsert(event) is True
    assert await repo.upsert(event) is False  # redelivery, correctly ignored

    count = await session.scalar(text("SELECT count(*) FROM inference_logs"))
    assert count == 1


async def test_error_and_cancelled_calls_are_logged_without_a_message(
    session: AsyncSession,
) -> None:
    """The rows the dashboards exist for are exactly the ones with no message."""
    repo = InferenceLogRepository(session)

    await repo.upsert(
        make_event(
            status=InferenceStatus.ERROR,
            error_type="RateLimitError",
            error_message="429 Too Many Requests",
        )
    )
    await repo.upsert(make_event(status=InferenceStatus.CANCELLED))

    logs = await repo.list_recent()
    assert {log.status for log in logs} == {InferenceStatus.ERROR, InferenceStatus.CANCELLED}
    assert all(log.message_id is None for log in logs)


async def test_a_log_survives_deletion_of_its_conversation(session: AsyncSession) -> None:
    """ON DELETE SET NULL, not CASCADE.

    Deleting a transcript must not erase the record of what it cost to produce.
    """
    conversations = ConversationRepository(session)
    conversation = await conversations.create()
    logs = InferenceLogRepository(session)
    event = make_event(conversation_id=conversation.id)
    await logs.upsert(event)

    await conversations.delete(conversation.id)
    await session.flush()

    surviving = await logs.get(event.id)
    assert surviving is not None
    assert surviving.conversation_id is None


async def test_negative_latency_is_rejected_by_the_database(session: AsyncSession) -> None:
    """A seconds-vs-milliseconds bug must fail loudly, not poison the percentiles."""
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO inference_logs "
                "(id, schema_version, provider, model, status, streamed, "
                " started_at, completed_at, latency_ms) "
                "VALUES (gen_random_uuid(), 1, 'mock', 'mock-1', 'success', false, "
                " now(), now(), -5)"
            )
        )


async def test_status_outside_the_domain_is_rejected(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO inference_logs "
                "(id, schema_version, provider, model, status, streamed, "
                " started_at, completed_at) "
                "VALUES (gen_random_uuid(), 1, 'mock', 'mock-1', 'ERROR', false, "
                " now(), now())"
            )
        )


async def test_listing_filters_and_orders_newest_first(session: AsyncSession) -> None:
    repo = InferenceLogRepository(session)
    base = datetime.now(UTC)

    for index, provider in enumerate(["mock", "anthropic", "mock"]):
        await repo.upsert(
            make_event(
                provider=provider,
                started_at=base + timedelta(seconds=index),
                completed_at=base + timedelta(seconds=index, milliseconds=50),
            )
        )

    mock_logs = await repo.list_recent(provider="mock")
    assert len(mock_logs) == 2
    assert mock_logs[0].started_at > mock_logs[1].started_at


async def test_before_cursor_pages_backwards_through_history(session: AsyncSession) -> None:
    repo = InferenceLogRepository(session)
    base = datetime.now(UTC)
    for index in range(5):
        await repo.upsert(
            make_event(
                started_at=base + timedelta(seconds=index),
                completed_at=base + timedelta(seconds=index, milliseconds=10),
            )
        )

    first_page = await repo.list_recent(limit=2)
    second_page = await repo.list_recent(limit=2, before=first_page[-1].started_at)

    assert len(second_page) == 2
    assert {log.id for log in first_page}.isdisjoint({log.id for log in second_page})
    assert second_page[0].started_at < first_page[-1].started_at
