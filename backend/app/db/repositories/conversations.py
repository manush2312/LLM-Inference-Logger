"""Conversation aggregate.

A conversation owns its messages: they are never created, ordered or read
independently of it. Modelling that as a single repository keeps the ordering
invariant (`seq`) enforceable in one place rather than trusting every caller.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, update

from app.core.errors import NotFoundError
from app.db.models import Conversation, Message
from app.db.repositories.base import Repository
from app.domain.enums import ConversationStatus, MessageRole

#: Conversations get an auto-title from the opening user message. Long enough
#: to be recognisable in a sidebar, short enough not to wrap.
_TITLE_MAX_CHARS = 60


class ConversationRepository(Repository):
    async def create(self, *, title: str | None = None) -> Conversation:
        conversation = Conversation(title=title)
        self._session.add(conversation)
        await self._session.flush()  # populate defaults without committing
        return conversation

    async def get(self, conversation_id: uuid.UUID) -> Conversation | None:
        return await self._session.get(Conversation, conversation_id)

    async def require(self, conversation_id: uuid.UUID) -> Conversation:
        """Fetch or raise. Saves every caller an identical `if is None` block."""
        conversation = await self.get(conversation_id)
        if conversation is None:
            raise NotFoundError("Conversation not found", conversation_id=str(conversation_id))
        return conversation

    async def list_recent(self, *, limit: int = 50, offset: int = 0) -> list[Conversation]:
        stmt = (
            select(Conversation)
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.scalars(stmt)).all())

    async def count(self) -> int:
        return (await self._session.scalar(select(func.count()).select_from(Conversation))) or 0

    async def append_message(
        self,
        conversation_id: uuid.UUID,
        *,
        role: MessageRole,
        content: str,
    ) -> Message:
        """Append a message, assigning the next `seq` atomically.

        The row lock serialises concurrent appends to the *same* conversation.
        Without it, two requests could read the same `max(seq)` and race: one
        insert would then violate the unique constraint and fail the user's
        request. Appends to different conversations are unaffected, so the
        contention is exactly as narrow as the invariant requires.
        """
        await self._session.execute(
            select(Conversation.id).where(Conversation.id == conversation_id).with_for_update()
        )

        next_seq = (
            await self._session.scalar(
                select(func.coalesce(func.max(Message.seq), -1) + 1).where(
                    Message.conversation_id == conversation_id
                )
            )
        ) or 0

        message = Message(conversation_id=conversation_id, role=role, content=content, seq=next_seq)
        self._session.add(message)
        await self._session.flush()

        # Appending is what makes a conversation "recently active"; the sidebar
        # sorts on this.
        await self._touch(conversation_id)
        return message

    async def messages(self, conversation_id: uuid.UUID) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.seq.asc())
        )
        return list((await self._session.scalars(stmt)).all())

    async def set_status(self, conversation_id: uuid.UUID, status: ConversationStatus) -> None:
        await self._session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(status=status, updated_at=func.now())
        )

    async def ensure_title(self, conversation_id: uuid.UUID, *, source: str) -> None:
        """Derive a title from the first user message, once.

        The `title IS NULL` predicate makes this idempotent and race-safe: a
        second concurrent call updates zero rows rather than overwriting.
        """
        title = source.strip().replace("\n", " ")[:_TITLE_MAX_CHARS] or "New conversation"
        await self._session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.title.is_(None))
            .values(title=title)
        )

    async def delete(self, conversation_id: uuid.UUID) -> None:
        conversation = await self.require(conversation_id)
        await self._session.delete(conversation)

    async def _touch(self, conversation_id: uuid.UUID) -> None:
        """Mark the conversation as recently active.

        `now()` in Postgres is *transaction* start time, not statement time.
        That is the behaviour we want: every row written while handling one
        request shares a single consistent timestamp, and the sidebar orders by
        request rather than by whichever statement happened to run last.
        """
        await self._session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(updated_at=func.now())
        )
