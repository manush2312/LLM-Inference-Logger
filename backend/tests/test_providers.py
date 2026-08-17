"""Provider abstraction behaviour.

All of it runs with no API keys and no network -- which is the point of having
a first-class mock provider rather than a test double.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.errors import ProviderError, ProviderNotConfiguredError
from app.domain.enums import MessageRole
from app.providers.base import ChatMessage, ChatRequest
from app.providers.mock import MockProvider
from app.providers.registry import ProviderRegistry


def request(model: str = "mock-instant", text: str = "What is Postgres?") -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role=MessageRole.USER, content=text)],
        model=model,
    )


# --- MockProvider ----------------------------------------------------------


async def test_streams_multiple_chunks_before_finishing() -> None:
    chunks = [chunk async for chunk in MockProvider().stream_chat(request())]

    assert len(chunks) > 1, "a single chunk would make TTFT meaningless"
    assert chunks[-1].finish_reason == "stop"


async def test_usage_arrives_on_the_final_chunk() -> None:
    chunks = [chunk async for chunk in MockProvider().stream_chat(request())]

    usage = chunks[-1].usage
    assert usage is not None
    assert usage.input_tokens is not None and usage.input_tokens > 0
    assert usage.output_tokens is not None and usage.output_tokens > 0


async def test_error_model_fails_after_emitting_output() -> None:
    """The interesting failure: partial output, then an error.

    A call that fails before emitting anything is easy; this one must still
    produce a log row carrying a TTFT and a partial `output_preview`.
    """
    emitted: list[str] = []

    with pytest.raises(ProviderError) as exc_info:
        async for chunk in MockProvider().stream_chat(request(model="mock-error")):
            emitted.append(chunk.delta_text)

    assert emitted, "must fail mid-stream, not before the first token"
    assert exc_info.value.provider == "mock"
    assert exc_info.value.retryable is True


async def test_complete_drains_the_stream_into_one_response() -> None:
    """Non-streaming shares the streaming code path, so it shares its telemetry."""
    response = await MockProvider().complete(request())

    assert response.text
    assert response.finish_reason == "stop"
    assert response.usage is not None
    assert response.metadata["provider_request_id"].startswith("mock_")


async def test_output_is_deterministic_for_the_same_input() -> None:
    """Demos and tests need repeatable output; real providers cannot give it."""
    first = await MockProvider().complete(request())
    second = await MockProvider().complete(request())

    assert first.text == second.text
    assert first.metadata == second.metadata


async def test_reply_reflects_the_latest_user_message() -> None:
    response = await MockProvider().complete(request(text="How do indexes work?"))

    assert "How do indexes work?" in response.text


# --- Registry --------------------------------------------------------------


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"anthropic_api_key": None, "openai_api_key": None}
    return Settings(**(base | overrides))  # type: ignore[arg-type]


def test_unconfigured_providers_are_not_registered() -> None:
    """Better a clear 400 than an auth failure from inside a vendor SDK."""
    registry = ProviderRegistry.from_settings(settings())

    assert registry.available() == ["mock"]


def test_providers_with_credentials_are_registered() -> None:
    registry = ProviderRegistry.from_settings(
        settings(anthropic_api_key="sk-ant-test", openai_api_key="sk-test")
    )

    assert registry.available() == ["anthropic", "mock", "openai"]


def test_requesting_an_unconfigured_provider_names_the_alternatives() -> None:
    registry = ProviderRegistry.from_settings(settings())

    with pytest.raises(ProviderNotConfiguredError) as exc_info:
        registry.get("anthropic")

    assert exc_info.value.context["available"] == ["mock"]


def test_an_unavailable_default_falls_back_rather_than_failing_startup() -> None:
    """A missing key is a misconfiguration, not a reason to refuse to boot."""
    registry = ProviderRegistry.from_settings(settings(default_provider="anthropic"))

    assert registry.default_name == "mock"


def test_resolve_model_applies_the_provider_default() -> None:
    registry = ProviderRegistry.from_settings(settings())

    assert registry.resolve_model(None, None) == ("mock", "mock")
    assert registry.resolve_model("mock", "mock-slow") == ("mock", "mock-slow")
