"""Chat orchestration.

One turn spans **two** transactions with the provider call in between, rather
than one transaction wrapped around everything. That shape is forced by the
logging requirement, not by preference:

* A provider failure must still produce an `inference_logs` row -- it is the
  single most important row the errors dashboard shows. Under one transaction
  the failure rolls the turn back, and any event referencing the conversation
  would point at a row that was never committed, so the worker's insert would
  hit a foreign-key violation.
* Committing the user message first makes `conversation_id` durable *before*
  the call is made, so every event -- success, error, or cancellation -- can
  safely reference it.
* It is also better behaviour: the user's message stays on screen when the
  model fails, instead of vanishing along with their typing.

The cost, stated plainly: a first message that fails leaves a conversation
holding a user message and no reply. That is visible and retryable, which is
preferable to a turn that silently disappears.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import Conversation, Message
from app.db.repositories.conversations import ConversationRepository
from app.db.session import Database
from app.domain.enums import MessageRole
from app.events.bus import EventBus
from app.instrumentation.redaction import Redactor
from app.instrumentation.wrapper import InstrumentedProvider
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
        database: Database,
        registry: ProviderRegistry,
        settings: Settings,
        bus: EventBus,
        redactor: Redactor,
    ) -> None:
        self._database = database
        self._registry = registry
        self._settings = settings
        self._bus = bus
        self._redactor = redactor

    async def send(
        self,
        *,
        content: str,
        conversation_id: uuid.UUID | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> ChatResult:
        # Resolved before anything is written: an unknown provider should be a
        # 400 against an untouched database, not a 400 that leaves a
        # conversation behind.
        provider_name, model_name = self._registry.resolve_model(provider, model)
        adapter = self._registry.get(provider_name)

        conversation, user_message, history = await self._record_user_turn(
            conversation_id=conversation_id, content=content
        )

        request = ChatRequest(
            messages=[ChatMessage(role=m.role, content=m.content) for m in history],
            model=model_name,
            max_output_tokens=self._settings.max_output_tokens,
        )

        # Substituted for the raw adapter: the service calls the same
        # `complete()` either way and has no idea it is being logged.
        instrumented = InstrumentedProvider(
            adapter,
            bus=self._bus,
            redactor=self._redactor,
            conversation_id=conversation.id,
            streamed=False,
        )

        log.info(
            "chat_turn_started",
            conversation_id=str(conversation.id),
            provider=provider_name,
            model=model_name,
            history_messages=len(history),
        )

        # Outside any transaction. A model call can take minutes; holding a
        # database connection and its locks open for that would exhaust the
        # pool under trivial concurrency.
        response = await instrumented.complete(request)

        assistant_message = await self._record_assistant_turn(conversation.id, response.text)

        return ChatResult(
            conversation=conversation,
            user_message=user_message,
            assistant_message=assistant_message,
            provider=provider_name,
            model=model_name,
        )

    async def _record_user_turn(
        self, *, conversation_id: uuid.UUID | None, content: str
    ) -> tuple[Conversation, Message, list[Message]]:
        """First transaction: durably record what the user said.

        Returns the history to send upstream, read back from the database
        rather than assembled in memory -- so a resumed conversation and a
        fresh one take exactly the same path.
        """
        async with self._database.session() as session:
            repo = ConversationRepository(session)

            conversation = (
                await repo.create()
                if conversation_id is None
                else await repo.require(conversation_id)
            )

            user_message = await repo.append_message(
                conversation.id, role=MessageRole.USER, content=content
            )
            await repo.ensure_title(conversation.id, source=content)

            history = await repo.messages(conversation.id)

        return conversation, user_message, history

    async def _record_assistant_turn(self, conversation_id: uuid.UUID, content: str) -> Message:
        """Second transaction: record the reply."""
        async with self._database.session() as session:
            return await ConversationRepository(session).append_message(
                conversation_id, role=MessageRole.ASSISTANT, content=content
            )
