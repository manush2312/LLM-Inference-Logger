"""Streaming and cancellation, end to end.

The cancellation tests deliberately drive the **real** trigger --
`stream_with_disconnect_cancellation` polling `request.is_disconnected()` and
cancelling the producer -- rather than calling `task.cancel()` directly. Those
are different mechanisms, and only the former is what runs in production. A
direct-cancel test would have passed against a watcher that cancels on every
poll tick, which measurably loses the event.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api.sse import stream_with_disconnect_cancellation
from app.core.config import EventBusBackend, Settings, get_settings
from app.domain.enums import InferenceStatus
from app.events.bus import InMemoryEventBus
from app.main import create_app
from app.services.chat import StreamingChatService

pytestmark = pytest.mark.integration


@pytest.fixture
async def app(integration_settings: Settings) -> AsyncIterator[FastAPI]:
    """Real Postgres, in-memory bus so published events are directly readable."""
    get_settings.cache_clear()
    settings = integration_settings.model_copy(
        update={
            "default_provider": "mock",
            "log_level": "WARNING",
            "event_bus_backend": EventBusBackend.MEMORY,
            # Pinned so a local `.env` cannot change which provider serves.
            "anthropic_api_key": None,
            "openai_api_key": None,
            "groq_api_key": None,
            "gemini_api_key": None,
            "ollama_enabled": False,
        }
    )
    application = create_app(settings)

    async with LifespanManager(application):
        async with application.state.database.session() as session:
            await session.execute(
                text(
                    "TRUNCATE conversations, messages, inference_logs, events_raw "
                    "RESTART IDENTITY CASCADE"
                )
            )
        yield application


@pytest.fixture
async def api(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


def bus_of(app: FastAPI) -> InMemoryEventBus:
    bus: InMemoryEventBus = app.state.event_bus
    return bus


def service_of(app: FastAPI) -> StreamingChatService:
    return StreamingChatService(
        database=app.state.database,
        registry=app.state.registry,
        settings=app.state.settings,
        bus=app.state.event_bus,
        redactor=app.state.redactor,
    )


def parse_sse(body: str) -> list[tuple[str, str]]:
    """Split a raw SSE body into (event, data) pairs."""
    frames = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        event = data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = line.removeprefix("data: ")
        frames.append((event, data))
    return frames


class _FakeDisconnectingRequest:
    """Stands in for a Starlette request whose client goes away.

    Counts polls so a test can assert the watcher stopped asking once it had
    already cancelled -- which is the guard that keeps a second `cancel()` from
    landing inside the wrapper's publish.
    """

    def __init__(self, *, disconnect_after_polls: int) -> None:
        self._threshold = disconnect_after_polls
        self.polls = 0

    async def is_disconnected(self) -> bool:
        self.polls += 1
        return self.polls > self._threshold


# --- Streaming happy path --------------------------------------------------


async def test_stream_emits_start_ttft_chunks_then_done(api: AsyncClient) -> None:
    response = await api.post(
        "/chat/stream", json={"content": "explain WAL", "model": "mock-instant"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    frames = parse_sse(response.text)
    names = [name for name, _ in frames]

    assert names[0] == "start"
    assert names[-1] == "done"
    assert "ttft" in names
    assert names.count("chunk") > 1, "a single chunk would not be streaming"


async def test_ttft_is_its_own_event_not_a_field_on_content(api: AsyncClient) -> None:
    """Metadata about the stream stays out of the stream's content.

    A client rendering text should never have to unpack timing data to find
    the words.
    """
    response = await api.post("/chat/stream", json={"content": "hello", "model": "mock-instant"})
    frames = parse_sse(response.text)

    ttft_frames = [data for name, data in frames if name == "ttft"]
    chunk_frames = [data for name, data in frames if name == "chunk"]

    assert len(ttft_frames) == 1, "TTFT is emitted exactly once"
    assert "ttft_ms" in ttft_frames[0]
    assert all("ttft" not in data for data in chunk_frames)
    # And it precedes the first content frame.
    names = [name for name, _ in frames]
    assert names.index("ttft") < names.index("chunk")


async def test_streamed_reply_is_persisted_and_resumable(api: AsyncClient) -> None:
    response = await api.post("/chat/stream", json={"content": "first", "model": "mock-instant"})
    frames = dict(parse_sse(response.text))
    conversation_id = _field(frames["start"], "conversation_id")

    transcript = (await api.get(f"/conversations/{conversation_id}")).json()
    assert [m["role"] for m in transcript["messages"]] == ["user", "assistant"]
    assert transcript["messages"][1]["content"]


async def test_streaming_marks_the_log_row_as_streamed(api: AsyncClient, app: FastAPI) -> None:
    await api.post("/chat/stream", json={"content": "hi", "model": "mock-instant"})

    event = bus_of(app).published[-1]
    assert event.streamed is True
    assert event.status is InferenceStatus.SUCCESS


# --- Failure after headers are sent ----------------------------------------


async def test_failure_arrives_as_an_error_event_on_a_200(api: AsyncClient) -> None:
    """Once headers are on the wire the status is fixed; errors go in-band."""
    response = await api.post("/chat/stream", json={"content": "boom", "model": "mock-error"})

    assert response.status_code == 200

    frames = parse_sse(response.text)
    names = [name for name, _ in frames]
    assert "error" in names
    assert "done" not in names, "a failed stream must not report completion"


async def test_failed_stream_still_logs_an_error_event(api: AsyncClient, app: FastAPI) -> None:
    await api.post("/chat/stream", json={"content": "boom", "model": "mock-error"})

    event = bus_of(app).published[-1]
    assert event.status is InferenceStatus.ERROR
    assert event.streamed is True


# --- Cancellation through the real disconnect path -------------------------


async def _consume_until_stopped(
    app: FastAPI, request: Any, *, model: str = "mock-cancel"
) -> list[str]:
    """Run one streamed turn through the production disconnect wrapper."""
    service = service_of(app)

    async def frames() -> AsyncIterator[str]:
        async for event in service.stream(content="stream forever", model=model):
            yield str(event)

    received: list[str] = []
    async for frame in stream_with_disconnect_cancellation(request, frames()):
        received.append(frame)
    return received


async def test_client_disconnect_cancels_the_generation_and_logs_it(app: FastAPI) -> None:
    """The production trigger: is_disconnected() polling, not a direct cancel.

    `mock-cancel` never terminates on its own, so the stream ending at all is
    proof the disconnect path stopped it.
    """
    request = _FakeDisconnectingRequest(disconnect_after_polls=2)

    received = await _consume_until_stopped(app, request)

    assert received, "some output was produced before the disconnect"

    # The cancellation had to reach the wrapper for this to exist.
    await asyncio.sleep(0.05)
    published = bus_of(app).published
    assert len(published) == 1, "exactly one event, even under cancellation"

    event = published[0]
    assert event.status is InferenceStatus.CANCELLED
    assert event.error_type is None, "a cancel is not a provider failure"
    assert event.ttft_ms is not None, "tokens flowed before the disconnect"
    assert event.output_preview, "partial output is retained"


async def test_watcher_stops_polling_once_it_has_cancelled(app: FastAPI) -> None:
    """The guard that keeps a second cancel() out of the wrapper's publish.

    An unguarded watcher would keep polling and keep calling cancel(); the
    second delivery lands mid-publish and destroys the event. Asserting the
    poll count stops growing pins the guard in place.
    """
    request = _FakeDisconnectingRequest(disconnect_after_polls=2)

    await _consume_until_stopped(app, request)
    polls_at_cancel = request.polls

    await asyncio.sleep(0.4)  # several more poll intervals would have elapsed

    assert request.polls == polls_at_cancel, "watcher kept polling after cancelling"


async def test_partial_output_survives_cancellation(app: FastAPI) -> None:
    """What the user already read stays in the transcript after a refresh."""
    request = _FakeDisconnectingRequest(disconnect_after_polls=3)

    await _consume_until_stopped(app, request)
    await asyncio.sleep(0.05)

    async with app.state.database.session() as session:
        rows = (
            await session.execute(text("SELECT role, content FROM messages ORDER BY seq"))
        ).all()

    assert [r.role for r in rows] == ["user", "assistant"]
    assert "still-streaming" in rows[1].content


async def test_cancelled_calls_report_no_token_counts(app: FastAPI) -> None:
    """Truthful nulls: a call that never finished has no final usage to report."""
    request = _FakeDisconnectingRequest(disconnect_after_polls=2)

    await _consume_until_stopped(app, request)
    await asyncio.sleep(0.05)

    event = bus_of(app).published[0]
    assert event.output_tokens is None
    assert event.finish_reason is None


def _field(data: str, key: str) -> str:
    import json

    value: str = json.loads(data)[key]
    return value


# --- Pre-stream failures must still be HTTP failures -----------------------


async def test_unknown_conversation_is_a_404_not_a_broken_stream(api: AsyncClient) -> None:
    """Found in the browser, not in a test.

    A page left open on a conversation that had since been deleted produced an
    unexplained `network_error`. The cause: validation ran inside the response
    generator, so `NotFoundError` was raised *after* StreamingResponse had
    already sent its 200 headers. Starlette then hit "Caught handled exception,
    but response already started", tore the connection down mid-flight, and the
    browser had nothing to report but a dead socket.

    Validation now runs before the response starts, so this is an ordinary 404.
    """
    missing = "00000000-0000-0000-0000-000000000000"

    response = await api.post(
        "/chat/stream",
        json={"conversation_id": missing, "content": "hi", "model": "mock-instant"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_unsupported_model_is_a_400_before_streaming(api: AsyncClient) -> None:
    """Same principle: knowable up front, so it gets a real status code."""
    response = await api.post(
        "/chat/stream", json={"content": "hi", "provider": "mock", "model": "llama3.2:1b"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_not_supported"


async def test_a_started_stream_never_raises_out_of_the_generator(api: AsyncClient) -> None:
    """Once bytes are flowing, every failure must be an in-band error frame.

    `mock-error` fails mid-generation; the response must still be a well-formed
    200 SSE stream that ends with an `error` event, never a truncated body.
    """
    response = await api.post("/chat/stream", json={"content": "boom", "model": "mock-error"})

    assert response.status_code == 200
    names = [name for name, _ in parse_sse(response.text)]
    assert names[-1] == "error", f"stream did not terminate cleanly: {names}"
