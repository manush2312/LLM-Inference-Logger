"""Chat orchestration.

This is the seam. Everything a chat turn involves -- resolving the provider,
loading history, calling the model, persisting both messages -- happens here,
so the HTTP layer stays a thin translation of JSON to arguments and the
instrumentation wrapper has exactly one call site to wrap.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import Conversation, Message
from app.db.repositories.conversations import ConversationRepository
from app.domain.enums import MessageRole
from app.providers.base import ChatMessage, ChatRequest
from app.providers.registry import ProviderRegistry

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ChatResult:
    conversation: Conversation
    user_message: Message
    assistant_message: Message
    provider: str
    model: str


class ChatService:
    def __init__(
        self,
        *,
        conversations: ConversationRepository,
        registry: ProviderRegistry,
        settings: Settings,
    ) -> None:
        self._conversations = conversations
        self._registry = registry
        self._settings = settings

    async def send(
        self,
        *,
        content: str,
        conversation_id: uuid.UUID | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> ChatResult:
        """Run one chat turn and persist both sides of it.

        The whole method runs inside the caller's transaction, so a failure
        anywhere rolls back cleanly rather than leaving a user message with no
        reply stranded in the transcript.
        """
        # Resolve before writing anything: an unknown provider should be a 400
        # with an empty database, not a 400 with an orphaned user message.
        provider_name, model_name = self._registry.resolve_model(provider, model)
        adapter = self._registry.get(provider_name)

        conversation = await self._resolve_conversation(conversation_id)

        user_message = await self._conversations.append_message(
            conversation.id, role=MessageRole.USER, content=content
        )
        await self._conversations.ensure_title(conversation.id, source=content)

        # Reloaded rather than accumulated in memory: the database is the only
        # thing that survives a restart, so reading history back is also what
        # proves a resumed conversation carries its full context.
        history = await self._conversations.messages(conversation.id)

        request = ChatRequest(
            messages=[ChatMessage(role=m.role, content=m.content) for m in history],
            model=model_name,
            max_output_tokens=self._settings.max_output_tokens,
        )

        log.info(
            "chat_turn_started",
            conversation_id=str(conversation.id),
            provider=provider_name,
            model=model_name,
            history_messages=len(history),
        )

        # The single call the instrumentation wrapper will wrap. Everything
        # needed to log the call -- provider, model, conversation -- is already
        # resolved at this point.
        response = await adapter.complete(request)

        assistant_message = await self._conversations.append_message(
            conversation.id, role=MessageRole.ASSISTANT, content=response.text
        )

        return ChatResult(
            conversation=conversation,
            user_message=user_message,
            assistant_message=assistant_message,
            provider=provider_name,
            model=model_name,
        )

    async def _resolve_conversation(self, conversation_id: uuid.UUID | None) -> Conversation:
        """Continue an existing conversation, or start one.

        A missing id means "new chat" rather than an error, so the first
        message of a session needs no separate create call.
        """
        if conversation_id is None:
            return await self._conversations.create()
        return await self._conversations.require(conversation_id)
