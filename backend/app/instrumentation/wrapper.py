"""Auto-instrumentation.

`InstrumentedProvider` wraps any `BaseProvider` and emits exactly one
`InferenceEvent` per call -- whether the call succeeded, failed, or was
abandoned. Because it wraps `stream_chat`, and `BaseProvider.complete()` is
built on `stream_chat`, non-streaming calls are instrumented by the same code
with no extra wiring. Adding a provider adds zero lines here.

The `try / except / finally` is the entire design. Every terminal outcome
converges on one `finally`, so the invariant "one call, one row" holds by
construction rather than by remembering to log on each path. That invariant is
what makes the error and cancellation dashboards trustworthy: a gap in this
stream would read as "no traffic" rather than "something went wrong".
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar

from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.domain.enums import InferenceStatus, MessageRole
from app.domain.events import InferenceEvent
from app.events.bus import EventBus
from app.instrumentation.redaction import Redactor
from app.providers.base import BaseProvider, ChatRequest, StreamChunk, TokenUsage

log = get_logger(__name__)


class InstrumentedProvider(BaseProvider):
    """A provider that logs itself.

    Deliberately a `BaseProvider` rather than a bare function: it is
    substitutable for the provider it wraps, so the calling service does not
    know or care whether it is instrumented.
    """

    name: ClassVar[str] = "instrumented"

    def __init__(
        self,
        inner: BaseProvider,
        *,
        bus: EventBus,
        redactor: Redactor,
        conversation_id: uuid.UUID | None = None,
        streamed: bool = False,
    ) -> None:
        self._inner = inner
        self._bus = bus
        self._redactor = redactor
        self._conversation_id = conversation_id
        self._streamed = streamed

    def default_model(self) -> str:
        return self._inner.default_model()

    def is_configured(self) -> bool:
        return self._inner.is_configured()

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        # Minted before the call so the event id is known regardless of how the
        # call ends -- and reused as the `inference_logs` primary key, which is
        # what makes redelivery idempotent rather than duplicate-producing.
        event_id = uuid.uuid4()

        started_at = datetime.now(UTC)
        # monotonic() for durations, wall clock for timestamps: a clock
        # adjustment mid-call must not be able to produce a negative latency.
        start = time.monotonic()

        ttft_ms: int | None = None
        chunks: list[str] = []
        usage = TokenUsage()
        finish_reason: str | None = None
        provider_metadata: dict[str, object] = {}

        status = InferenceStatus.SUCCESS
        error_type: str | None = None
        error_message: str | None = None

        try:
            async for chunk in self._inner.stream_chat(request):
                if ttft_ms is None and chunk.delta_text:
                    # Time to *first token*, not first event: providers emit
                    # metadata-only events before any text, and counting those
                    # would report a TTFT no user ever perceived.
                    ttft_ms = int((time.monotonic() - start) * 1000)

                chunks.append(chunk.delta_text)
                usage = _merge_usage(usage, chunk.usage)
                finish_reason = chunk.finish_reason or finish_reason
                provider_metadata.update(chunk.metadata)

                yield chunk

        except BaseException as exc:
            # `BaseException`, so cancellation is caught: CancelledError
            # inherits from BaseException, and an `except Exception` here would
            # let every cancelled call slip past unlogged -- silently emptying
            # the one dashboard panel that exists to show them.
            status, error_type, error_message = _classify(exc)
            raise

        finally:
            completed_at = datetime.now(UTC)
            latency_ms = int((time.monotonic() - start) * 1000)

            event = InferenceEvent(
                id=event_id,
                conversation_id=self._conversation_id,
                # Left unset: the assistant message is written after this call
                # returns, so publishing its id here would race the worker into
                # a foreign-key violation. Correlate on conversation_id and
                # started_at instead -- see docs/ARCHITECTURE.md.
                message_id=None,
                provider=self._inner.name,
                model=request.model,
                status=status,
                streamed=self._streamed,
                started_at=started_at,
                completed_at=completed_at,
                latency_ms=latency_ms,
                ttft_ms=ttft_ms,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                # Redacted here, in the API process, so unredacted content
                # never reaches Redis, the worker, or the database.
                input_preview=self._redactor.preview(_last_user_text(request)),
                output_preview=self._redactor.preview("".join(chunks)),
                error_type=error_type,
                error_message=error_message,
                finish_reason=finish_reason,
                raw_metadata=dict(provider_metadata) or None,
            )

            # Awaited rather than fired into a background task: the bus itself
            # is bounded and never raises, so this adds at most one short
            # timeout to the request tail while keeping the publish
            # deterministic and testable.
            await self._bus.publish(event)


def _merge_usage(current: TokenUsage, incoming: TokenUsage | None) -> TokenUsage:
    """Keep the last non-null value per field.

    Providers report the two halves at different points -- Anthropic sends
    input tokens on `message_start` and output tokens at the end -- so naively
    replacing the whole object would discard whichever half arrived first.
    """
    if incoming is None:
        return current

    return TokenUsage(
        input_tokens=incoming.input_tokens
        if incoming.input_tokens is not None
        else current.input_tokens,
        output_tokens=incoming.output_tokens
        if incoming.output_tokens is not None
        else current.output_tokens,
    )


def _classify(exc: BaseException) -> tuple[InferenceStatus, str | None, str]:
    """Map a terminal exception to a logged status.

    Cancellation is not a failure. Conflating the two would inflate the error
    rate every time a user closed a tab, and mask real provider failures behind
    that noise.
    """
    if isinstance(exc, asyncio.CancelledError):
        return InferenceStatus.CANCELLED, None, "Cancelled by client"

    if isinstance(exc, ProviderError):
        # Prefer the vendor exception class the adapter recorded over our own
        # wrapper type -- "RateLimitError" is actionable, "ProviderError" is not.
        recorded = exc.context.get("error_type")
        return InferenceStatus.ERROR, str(recorded or type(exc).__name__), exc.message

    return InferenceStatus.ERROR, type(exc).__name__, str(exc) or type(exc).__name__


def _last_user_text(request: ChatRequest) -> str | None:
    """Log the newest turn, not the whole history.

    Storing the full prompt would duplicate the entire conversation on every
    call -- quadratic storage growth, and the transcript is already in
    `messages`.
    """
    for message in reversed(request.messages):
        if message.role is MessageRole.USER:
            return message.content
    return None
