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

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.core.config import Settings
from app.core.errors import AppError, ProviderError
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
            # Clamped per provider: the configured budget is what we want, the
            # provider's cap is what it will accept.
            max_output_tokens=adapter.clamp_output_tokens(self._settings.max_output_tokens),
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


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
#
# Domain-level stream events. The API layer maps these onto SSE frames and does
# nothing else, so the wire format can change without touching orchestration --
# and the service stays testable without an HTTP client.


@dataclass(frozen=True, slots=True)
class StreamStarted:
    conversation_id: uuid.UUID
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class StreamFirstToken:
    """Emitted once, the moment the first token of real text arrives.

    Its own event rather than a field on the first content chunk: timing is
    metadata about the stream, not part of it, and a client rendering text
    should never have to unpack measurements to find the words.
    """

    ttft_ms: int


@dataclass(frozen=True, slots=True)
class StreamDelta:
    text: str


@dataclass(frozen=True, slots=True)
class StreamCompleted:
    message_id: uuid.UUID
    latency_ms: int


@dataclass(frozen=True, slots=True)
class StreamFailed:
    """A failure that happened after the response headers were already sent.

    Once streaming has begun the HTTP status is fixed, so a failure has to be
    reported in-band. Returning a 502 is not available any more -- the client
    has long since seen a 200.
    """

    code: str
    message: str


ChatStreamEvent = StreamStarted | StreamFirstToken | StreamDelta | StreamCompleted | StreamFailed


class StreamingChatService(ChatService):
    """`ChatService` with a streaming turn.

    Same two-transaction shape as `send()`: the user turn is committed before
    the provider is called, so cancellation and failure both leave a coherent
    transcript and a loggable conversation id.
    """

    async def prepare(
        self,
        *,
        conversation_id: uuid.UUID | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> tuple[str, str]:
        """Validate everything knowable *before* the response starts.

        Called by the endpoint ahead of `StreamingResponse`, because anything
        that raises once headers are on the wire cannot become an HTTP status
        any more. Starlette's handler then hits "Caught handled exception, but
        response already started", the connection is torn down mid-flight, and
        the browser reports a bare network error with no explanation --
        observed exactly that for a request naming a deleted conversation.

        So the checks that can be made up front are made up front, and get real
        status codes: 400 for an unsupported model, 404 for a conversation that
        does not exist.
        """
        provider_name, model_name = self._registry.resolve_model(provider, model)

        if conversation_id is not None:
            async with self._database.session() as session:
                await ConversationRepository(session).require(conversation_id)

        return provider_name, model_name

    async def stream(
        self,
        *,
        content: str,
        conversation_id: uuid.UUID | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        provider_name, model_name = self._registry.resolve_model(provider, model)
        adapter = self._registry.get(provider_name)

        try:
            conversation, _user_message, history = await self._record_user_turn(
                conversation_id=conversation_id, content=content
            )
        except AppError as exc:
            # Belt and braces. `prepare()` should have caught this already, but
            # an exception escaping after the headers are sent produces an
            # unexplained dropped connection rather than any kind of error --
            # so this path must never be able to raise, only report.
            yield StreamFailed(code=exc.code, message=exc.message)
            return

        yield StreamStarted(
            conversation_id=conversation.id, provider=provider_name, model=model_name
        )

        request = ChatRequest(
            messages=[ChatMessage(role=m.role, content=m.content) for m in history],
            model=model_name,
            max_output_tokens=adapter.clamp_output_tokens(self._settings.max_output_tokens),
        )
        instrumented = InstrumentedProvider(
            adapter,
            bus=self._bus,
            redactor=self._redactor,
            conversation_id=conversation.id,
            streamed=True,
        )

        parts: list[str] = []
        start = time.monotonic()
        first_token_seen = False

        try:
            async for chunk in instrumented.stream_chat(request):
                if not chunk.delta_text:
                    continue

                if not first_token_seen:
                    first_token_seen = True
                    yield StreamFirstToken(ttft_ms=int((time.monotonic() - start) * 1000))

                parts.append(chunk.delta_text)
                yield StreamDelta(text=chunk.delta_text)

        except asyncio.CancelledError:
            # The user stopped the generation. Keep whatever they already saw
            # so the transcript matches the screen after a refresh, then let
            # cancellation continue propagating.
            await self._persist_partial(conversation.id, parts)
            raise

        except ProviderError as exc:
            # Reported in-band: headers are long gone, so there is no status
            # code left to set. The wrapper has already logged it.
            await self._persist_partial(conversation.id, parts)
            yield StreamFailed(code=exc.code, message=exc.message)
            return

        except AppError as exc:
            yield StreamFailed(code=exc.code, message=exc.message)
            return

        assistant_message = await self._record_assistant_turn(conversation.id, "".join(parts))
        yield StreamCompleted(
            message_id=assistant_message.id,
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    async def _persist_partial(self, conversation_id: uuid.UUID, parts: list[str]) -> None:
        """Save partial output produced before an interruption.

        Shielded for the same reason the event publish is: this runs while the
        task is being cancelled, and a second cancellation would otherwise kill
        the write mid-flight, losing text the user already read on screen.
        """
        text = "".join(parts).strip()
        if not text:
            return

        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(
                asyncio.create_task(self._record_assistant_turn(conversation_id, text))
            )
