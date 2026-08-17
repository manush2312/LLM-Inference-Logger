"""Instrumentation invariants.

The single property everything else rests on: one provider call produces
exactly one event, whatever happens to the call. If that ever fails, the
dashboards stop describing reality -- and they fail *silently*, because a
missing row looks identical to no traffic.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from app.core.errors import ProviderError
from app.domain.enums import InferenceStatus, MessageRole
from app.events.bus import InMemoryEventBus
from app.instrumentation.redaction import Redactor
from app.instrumentation.wrapper import InstrumentedProvider
from app.providers.base import BaseProvider, ChatMessage, ChatRequest, StreamChunk, TokenUsage
from app.providers.mock import MockProvider


def request(model: str = "mock-instant", text: str = "hello") -> ChatRequest:
    return ChatRequest(messages=[ChatMessage(role=MessageRole.USER, content=text)], model=model)


def wrap(inner: BaseProvider, bus: InMemoryEventBus, **kwargs: object) -> InstrumentedProvider:
    return InstrumentedProvider(
        inner,
        bus=bus,
        redactor=Redactor(max_chars=500),
        **kwargs,  # type: ignore[arg-type]
    )


class _NeverEndingProvider(BaseProvider):
    """Streams until cancelled -- stands in for a slow real model."""

    name = "never-ending"

    def default_model(self) -> str:
        return "never"

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        while True:
            yield StreamChunk(delta_text="tick ")
            await asyncio.sleep(0.01)


class _UsageSplitProvider(BaseProvider):
    """Reports input and output tokens on different chunks, as Anthropic does."""

    name = "split-usage"

    def default_model(self) -> str:
        return "split"

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(usage=TokenUsage(input_tokens=11))
        yield StreamChunk(delta_text="hi ")
        yield StreamChunk(finish_reason="stop", usage=TokenUsage(output_tokens=7))


# --- One call, one event ---------------------------------------------------


async def test_success_emits_exactly_one_event() -> None:
    bus = InMemoryEventBus()

    await wrap(MockProvider(), bus).complete(request())

    assert len(bus.published) == 1
    event = bus.published[0]
    assert event.status is InferenceStatus.SUCCESS
    assert event.provider == "mock"
    assert event.error_message is None


async def test_provider_failure_still_emits_an_event() -> None:
    """The row the errors dashboard exists to show."""
    bus = InMemoryEventBus()

    with pytest.raises(ProviderError):
        await wrap(MockProvider(), bus).complete(request(model="mock-error"))

    assert len(bus.published) == 1
    event = bus.published[0]
    assert event.status is InferenceStatus.ERROR
    assert event.error_message
    assert event.output_preview, "partial output before the failure must be kept"


async def test_cancellation_emits_an_event_and_is_not_an_error() -> None:
    """Cancellation must not be logged as a failure.

    Conflating the two would spike the error rate every time a user closed a
    tab, and bury real provider failures in that noise.
    """
    bus = InMemoryEventBus()
    provider = wrap(_NeverEndingProvider(), bus)

    async def consume() -> None:
        async for _ in provider.stream_chat(request(model="never")):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(bus.published) == 1
    event = bus.published[0]
    assert event.status is InferenceStatus.CANCELLED
    assert event.error_type is None
    assert event.ttft_ms is not None, "tokens arrived before the cancel, so TTFT is real"


async def test_every_event_carries_a_unique_id() -> None:
    """Ids are per call, so redelivery dedupes without collapsing real calls."""
    bus = InMemoryEventBus()
    provider = wrap(MockProvider(), bus)

    for _ in range(3):
        await provider.complete(request())

    assert len({event.id for event in bus.published}) == 3


# --- Measurement -----------------------------------------------------------


async def test_timings_are_recorded_and_internally_consistent() -> None:
    bus = InMemoryEventBus()

    await wrap(MockProvider(), bus).complete(request(model="mock"))

    event = bus.published[0]
    assert event.latency_ms is not None and event.latency_ms > 0
    assert event.ttft_ms is not None
    assert event.ttft_ms <= event.latency_ms, "first token cannot follow completion"
    assert event.completed_at >= event.started_at


async def test_usage_reported_across_chunks_is_merged_not_overwritten() -> None:
    """Anthropic sends input tokens first and output tokens last.

    Replacing the usage object per chunk would silently drop whichever half
    arrived first, leaving half the cost data null for one provider.
    """
    bus = InMemoryEventBus()

    await wrap(_UsageSplitProvider(), bus).complete(request(model="split"))

    event = bus.published[0]
    assert event.input_tokens == 11
    assert event.output_tokens == 7


async def test_conversation_is_recorded_on_the_event() -> None:
    import uuid

    bus = InMemoryEventBus()
    conversation_id = uuid.uuid4()

    await wrap(MockProvider(), bus, conversation_id=conversation_id).complete(request())

    assert bus.published[0].conversation_id == conversation_id


# --- The wrapper must not change behaviour ---------------------------------


async def test_instrumentation_does_not_alter_the_response() -> None:
    """Observing a call must not change it."""
    bare = await MockProvider().complete(request())
    instrumented = await wrap(MockProvider(), InMemoryEventBus()).complete(request())

    assert instrumented.text == bare.text
    assert instrumented.usage == bare.usage
    assert instrumented.finish_reason == bare.finish_reason


async def test_a_broken_bus_never_breaks_the_call() -> None:
    """A logging outage must cost telemetry, not availability."""

    class _BrokenBus(InMemoryEventBus):
        async def publish(self, event: object) -> bool:  # type: ignore[override]
            return False

    response = await wrap(MockProvider(), _BrokenBus()).complete(request())

    assert response.text, "the user still got their answer"


# --- Redaction happens before the event leaves the process -----------------


async def test_previews_are_redacted_before_publication() -> None:
    """Unredacted content must never reach the bus, let alone the database."""
    bus = InMemoryEventBus()
    secret = "email me at manush@example.com or call +1 415 555 0199"

    await wrap(MockProvider(), bus).complete(request(text=secret))

    preview = bus.published[0].input_preview or ""
    assert "manush@example.com" not in preview
    assert "[REDACTED_EMAIL]" in preview
    assert "555" not in preview


async def test_only_the_newest_turn_is_previewed() -> None:
    """Storing the whole prompt would duplicate the transcript on every call."""
    bus = InMemoryEventBus()
    provider = wrap(MockProvider(), bus)

    await provider.complete(
        ChatRequest(
            messages=[
                ChatMessage(role=MessageRole.USER, content="ancient history"),
                ChatMessage(role=MessageRole.ASSISTANT, content="a reply"),
                ChatMessage(role=MessageRole.USER, content="the newest question"),
            ],
            model="mock-instant",
        )
    )

    assert bus.published[0].input_preview == "the newest question"


async def test_generator_close_is_recorded_as_cancelled() -> None:
    """Locks the `GeneratorExit` branch of `_classify`.

    Starlette signals a client disconnect by calling `aclose()` on the response
    generator, which raises `GeneratorExit` -- not `CancelledError`. In live
    runs this is the path that actually fires, ahead of the `is_disconnected()`
    watcher, so if it were classified as an error every closed browser tab
    would be filed as a provider failure.

    This is also the cheapest branch in the system to break silently: it
    depends on Starlette implementation behaviour rather than a documented ASGI
    guarantee, and breaking it produces no crash -- only a slow drift in what
    the dashboard claims. Tested separately from the disconnect-watcher test,
    which drives `CancelledError` instead and would still pass.
    """
    bus = InMemoryEventBus()
    provider = wrap(_NeverEndingProvider(), bus)

    stream = provider.stream_chat(request(model="never"))
    async for _ in stream:
        break

    await stream.aclose()

    assert len(bus.published) == 1
    event = bus.published[0]
    assert event.status is InferenceStatus.CANCELLED, (
        "GeneratorExit was not treated as a cancellation"
    )
    assert event.error_type is None


async def test_shutdown_drain_waits_for_inflight_publishes() -> None:
    """`shield` protects a publish from its caller, not from the process exiting.

    A rolling deploy sends SIGTERM mid-publish; without an explicit drain the
    loop closes and the event vanishes -- the same silent-telemetry-loss class
    as the double-cancel bug, triggered by pod termination instead.
    """
    from app.instrumentation.wrapper import drain_pending_publishes

    class _SlowBus(InMemoryEventBus):
        async def publish(self, event: object) -> bool:  # type: ignore[override]
            await asyncio.sleep(0.05)
            return await super().publish(event)  # type: ignore[arg-type]

    bus = _SlowBus()
    await wrap(MockProvider(), bus).complete(request())

    dropped = await drain_pending_publishes(grace_seconds=1.0)

    assert dropped == 0
    assert len(bus.published) == 1
