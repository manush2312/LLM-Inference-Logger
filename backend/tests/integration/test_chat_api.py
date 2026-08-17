"""End-to-end API behaviour against a real database.

The two tests that matter here are the multi-turn ones. A chat UI looks
perfectly healthy while silently dropping conversation history -- each reply is
fluent, it just has amnesia. One request/response proves nothing about that;
only turn two referencing turn one does.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
def api_settings(integration_settings: Settings) -> Settings:
    """Real Postgres, mock provider, instant responses."""
    return integration_settings.model_copy(
        update={"default_provider": "mock", "log_level": "WARNING"}
    )


@pytest.fixture
async def api(api_settings: Settings) -> AsyncIterator[AsyncClient]:
    get_settings.cache_clear()
    app = create_app(api_settings)

    async with LifespanManager(app):
        database = app.state.database
        async with database.session() as session:
            await session.execute(
                text(
                    "TRUNCATE conversations, messages, inference_logs, events_raw "
                    "RESTART IDENTITY CASCADE"
                )
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


def turn_number(reply: str) -> int:
    """The mock reports its own view of conversation depth; read it back."""
    match = re.search(r"turn (\d+) of the conversation", reply)
    assert match, f"mock reply did not report a turn number: {reply!r}"
    return int(match.group(1))


def prior_messages(reply: str) -> int:
    match = re.search(r"received (\d+) earlier message", reply)
    assert match, f"mock reply did not report context depth: {reply!r}"
    return int(match.group(1))


# --- The M2 bar ------------------------------------------------------------


async def test_second_turn_receives_the_first_turn_as_context(api: AsyncClient) -> None:
    """Turn two must see turn one. This is what "multi-turn" actually means.

    Asserted via the provider's own report of what it received, not via our
    intent to send it -- the failure mode is history being dropped somewhere
    between the database and the adapter.
    """
    first = await api.post("/chat", json={"content": "My name is Manush.", "model": "mock-instant"})
    assert first.status_code == 200
    conversation_id = first.json()["conversation_id"]

    first_reply = first.json()["assistant_message"]["content"]
    assert turn_number(first_reply) == 1
    assert prior_messages(first_reply) == 0

    second = await api.post(
        "/chat",
        json={
            "conversation_id": conversation_id,
            "content": "What is my name?",
            "model": "mock-instant",
        },
    )
    assert second.status_code == 200

    second_reply = second.json()["assistant_message"]["content"]
    assert turn_number(second_reply) == 2, "history was not carried into turn two"
    # user 1 + assistant 1 + user 2 = 3 messages, of which 2 precede the newest.
    assert prior_messages(second_reply) == 2


async def test_conversation_survives_a_client_restart(api: AsyncClient) -> None:
    """A refreshed browser must resume the same conversation, not fork one.

    Simulates the refresh literally: forget everything except the id, refetch,
    then continue. The turn count proves the resumed history was real.
    """
    started = await api.post("/chat", json={"content": "First message", "model": "mock-instant"})
    conversation_id = started.json()["conversation_id"]

    # --- the "refresh": client state is gone, only the id survives ---
    reloaded = await api.get(f"/conversations/{conversation_id}")
    assert reloaded.status_code == 200

    body = reloaded.json()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert [m["seq"] for m in body["messages"]] == [0, 1]
    assert body["title"] == "First message"

    resumed = await api.post(
        "/chat",
        json={
            "conversation_id": conversation_id,
            "content": "Second message",
            "model": "mock-instant",
        },
    )
    assert turn_number(resumed.json()["assistant_message"]["content"]) == 2

    final = await api.get(f"/conversations/{conversation_id}")
    assert [m["seq"] for m in final.json()["messages"]] == [0, 1, 2, 3]


# --- Conversation lifecycle ------------------------------------------------


async def test_chat_without_a_conversation_id_starts_one(api: AsyncClient) -> None:
    """The first message of a session needs no separate create call."""
    response = await api.post("/chat", json={"content": "Hello", "model": "mock-instant"})

    assert response.status_code == 200
    assert response.json()["conversation_id"]

    listed = await api.get("/conversations")
    assert listed.json()["total"] == 1


async def test_conversations_are_listed_most_recent_first(api: AsyncClient) -> None:
    for content in ("older", "newer"):
        await api.post("/chat", json={"content": content, "model": "mock-instant"})

    titles = [c["title"] for c in (await api.get("/conversations")).json()["items"]]
    assert titles == ["newer", "older"]


async def test_transcript_is_ordered_by_seq_not_insertion(api: AsyncClient) -> None:
    started = await api.post("/chat", json={"content": "one", "model": "mock-instant"})
    conversation_id = started.json()["conversation_id"]
    for content in ("two", "three"):
        await api.post(
            "/chat",
            json={
                "conversation_id": conversation_id,
                "content": content,
                "model": "mock-instant",
            },
        )

    messages = (await api.get(f"/conversations/{conversation_id}")).json()["messages"]
    assert [m["seq"] for m in messages] == list(range(len(messages)))
    assert [m["content"] for m in messages if m["role"] == "user"] == ["one", "two", "three"]


async def test_deleting_a_conversation_removes_it(api: AsyncClient) -> None:
    started = await api.post("/chat", json={"content": "temporary", "model": "mock-instant"})
    conversation_id = started.json()["conversation_id"]

    assert (await api.delete(f"/conversations/{conversation_id}")).status_code == 204
    assert (await api.get(f"/conversations/{conversation_id}")).status_code == 404


# --- Failure paths ---------------------------------------------------------


async def test_unknown_conversation_returns_404_with_a_machine_code(api: AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    response = await api.post(
        "/chat", json={"conversation_id": missing, "content": "hi", "model": "mock-instant"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_unconfigured_provider_is_rejected_before_anything_is_written(
    api: AsyncClient,
) -> None:
    """A bad provider must not leave an orphaned user message behind."""
    response = await api.post(
        "/chat", json={"content": "hi", "provider": "definitely-not-configured"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "provider_not_configured"
    assert (await api.get("/conversations")).json()["total"] == 0


async def test_empty_message_is_rejected(api: AsyncClient) -> None:
    assert (await api.post("/chat", json={"content": ""})).status_code == 422


async def test_provider_errors_surface_as_502_and_roll_back(api: AsyncClient) -> None:
    """A failed model call must not strand a user message with no reply."""
    response = await api.post("/chat", json={"content": "trigger failure", "model": "mock-error"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_error"
    assert (await api.get("/conversations")).json()["total"] == 0


# --- Provider discovery ----------------------------------------------------


async def test_providers_endpoint_reports_what_is_actually_configured(api: AsyncClient) -> None:
    body = (await api.get("/providers")).json()

    assert body["default"] == "mock"
    assert [p["name"] for p in body["items"]] == ["mock"]
    assert body["items"][0]["is_default"] is True
