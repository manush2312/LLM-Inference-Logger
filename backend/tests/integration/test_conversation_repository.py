from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.repositories.conversations import ConversationRepository
from app.db.session import Database
from app.domain.enums import ConversationStatus, MessageRole

pytestmark = pytest.mark.integration


async def test_messages_are_assigned_contiguous_sequence_numbers(session: AsyncSession) -> None:
    repo = ConversationRepository(session)
    conversation = await repo.create()

    for i in range(3):
        await repo.append_message(conversation.id, role=MessageRole.USER, content=f"message {i}")

    messages = await repo.messages(conversation.id)
    assert [m.seq for m in messages] == [0, 1, 2]
    assert [m.content for m in messages] == ["message 0", "message 1", "message 2"]


async def test_concurrent_appends_do_not_collide_on_seq(database: Database) -> None:
    """The row lock must serialise appends to the same conversation.

    Without `SELECT ... FOR UPDATE`, both writers read the same `max(seq)` and
    one insert violates the unique constraint -- surfacing to a user as a
    failed chat request.
    """
    async with database.session() as setup_session:
        conversation = await ConversationRepository(setup_session).create()

    async def append(content: str) -> None:
        async with database.session() as s:
            await ConversationRepository(s).append_message(
                conversation.id, role=MessageRole.USER, content=content
            )

    await asyncio.gather(*(append(f"concurrent {i}") for i in range(5)))

    async with database.session() as s:
        messages = await ConversationRepository(s).messages(conversation.id)

    assert sorted(m.seq for m in messages) == [0, 1, 2, 3, 4]


async def test_ensure_title_only_applies_once(session: AsyncSession) -> None:
    repo = ConversationRepository(session)
    conversation = await repo.create()

    await repo.ensure_title(conversation.id, source="What is the capital of France?")
    await repo.ensure_title(conversation.id, source="a completely different question")

    await session.refresh(conversation)
    assert conversation.title == "What is the capital of France?"


async def test_ensure_title_truncates_and_flattens_newlines(session: AsyncSession) -> None:
    repo = ConversationRepository(session)
    conversation = await repo.create()

    await repo.ensure_title(conversation.id, source="line one\nline two " + "x" * 200)

    await session.refresh(conversation)
    assert conversation.title is not None
    assert len(conversation.title) <= 60
    assert "\n" not in conversation.title


async def test_appending_a_message_marks_the_conversation_recently_active(
    database: Database,
) -> None:
    """Each append must advance `updated_at`, which is what orders the sidebar.

    Deliberately spans two transactions, because that is how it happens in
    production -- one request creates, a later request appends. Within a single
    transaction Postgres' `now()` is frozen at transaction start, so a
    same-transaction assertion would test the fixture rather than the feature.
    """
    async with database.session() as s:
        conversation = await ConversationRepository(s).create()
        original_updated_at = conversation.updated_at

    async with database.session() as s:
        await ConversationRepository(s).append_message(
            conversation.id, role=MessageRole.USER, content="hi"
        )

    async with database.session() as s:
        refreshed = await ConversationRepository(s).require(conversation.id)
        assert refreshed.updated_at > original_updated_at


async def test_deleting_a_conversation_cascades_to_its_messages(session: AsyncSession) -> None:
    repo = ConversationRepository(session)
    conversation = await repo.create()
    await repo.append_message(conversation.id, role=MessageRole.USER, content="hi")

    await repo.delete(conversation.id)
    await session.flush()

    assert await repo.get(conversation.id) is None
    assert await repo.messages(conversation.id) == []


async def test_require_raises_for_a_missing_conversation(session: AsyncSession) -> None:
    import uuid

    with pytest.raises(NotFoundError):
        await ConversationRepository(session).require(uuid.uuid4())


async def test_list_orders_by_most_recently_updated(database: Database) -> None:
    async with database.session() as s:
        repo = ConversationRepository(s)
        first = await repo.create(title="first")
        await asyncio.sleep(0.01)
        second = await repo.create(title="second")

    async with database.session() as s:
        # Touching the older conversation must float it to the top.
        await ConversationRepository(s).append_message(
            first.id, role=MessageRole.USER, content="bump"
        )

    async with database.session() as s:
        listed = await ConversationRepository(s).list_recent()

    assert [c.id for c in listed] == [first.id, second.id]
    assert listed[1].id == second.id


async def test_status_transitions_are_persisted(session: AsyncSession) -> None:
    repo = ConversationRepository(session)
    conversation = await repo.create()

    await repo.set_status(conversation.id, ConversationStatus.CANCELLED)

    await session.refresh(conversation)
    assert conversation.status is ConversationStatus.CANCELLED
