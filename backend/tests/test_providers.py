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
from app.providers.openai_compatible import (
    GEMINI_BASE_URL,
    GROQ_BASE_URL,
    GeminiProvider,
    GroqProvider,
)
from app.providers.openai_provider import OpenAIProvider
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


# --- OpenAI-compatible backends --------------------------------------------


def test_compatible_providers_keep_distinct_registry_names() -> None:
    """Groq must not masquerade as `openai`.

    `inference_logs.provider` is what every dashboard panel groups by, so a
    shared name would make two vendors' traffic indistinguishable -- the one
    thing a multi-provider observability tool must not lose.
    """
    registry = ProviderRegistry.from_settings(
        settings(
            groq_api_key="gsk_test",
            gemini_api_key="AIza_test",
            openai_api_key="sk-test",
        )
    )

    assert registry.available() == ["gemini", "groq", "mock", "openai"]


def test_compatible_providers_are_omitted_without_a_key() -> None:
    registry = ProviderRegistry.from_settings(settings())

    assert "groq" not in registry.available()
    assert "gemini" not in registry.available()


def test_each_compatible_provider_points_at_its_own_endpoint() -> None:
    """A wrong base URL sends requests to the wrong vendor with the wrong key."""
    groq = GroqProvider(
        api_key="gsk_test", base_url=GROQ_BASE_URL, default_model="llama-3.3-70b-versatile"
    )
    gemini = GeminiProvider(
        api_key="AIza_test", base_url=GEMINI_BASE_URL, default_model="gemini-2.0-flash"
    )

    assert "groq.com" in str(groq._client.base_url)  # type: ignore[union-attr]
    assert "googleapis.com" in str(gemini._client.base_url)  # type: ignore[union-attr]


def test_gemini_omits_stream_usage_options() -> None:
    """Gemini's compatibility layer rejects `stream_options`.

    Sending it anyway would 400 every call. Omitting it costs token counts on
    this provider only, which the schema already models as nullable.
    """
    gemini = GeminiProvider(
        api_key="AIza_test", base_url=GEMINI_BASE_URL, default_model="gemini-2.0-flash"
    )
    groq = GroqProvider(
        api_key="gsk_test", base_url=GROQ_BASE_URL, default_model="llama-3.3-70b-versatile"
    )

    assert gemini._usage_kwargs() == {}
    assert groq._usage_kwargs() == {"stream_options": {"include_usage": True}}


def test_openai_itself_still_requests_stream_usage() -> None:
    """The flag defaults to today's behaviour; adding it changed nothing."""
    openai_provider = OpenAIProvider("sk-test", "gpt-4o")

    assert openai_provider._usage_kwargs() == {"stream_options": {"include_usage": True}}


# --- Ollama ----------------------------------------------------------------


def test_ollama_requires_an_explicit_opt_in() -> None:
    """Not probed at startup.

    Registering a local server we cannot reach would turn a clear "not
    configured" error into a connection failure surfacing mid-stream.
    """
    assert "ollama" not in ProviderRegistry.from_settings(settings()).available()
    assert "ollama" in ProviderRegistry.from_settings(settings(ollama_enabled=True)).available()


def test_ollama_needs_no_api_key() -> None:
    """It ignores credentials, but the OpenAI client refuses to construct
    without one -- so a placeholder is supplied rather than left empty, which
    would make `is_configured()` wrongly report False."""
    registry = ProviderRegistry.from_settings(settings(ollama_enabled=True))

    assert registry.get("ollama").is_configured() is True


# --- What the UI is offered ------------------------------------------------


def test_providers_advertise_the_models_the_picker_offers() -> None:
    """The dropdown is built from this, so it can never offer a 400."""
    registry = ProviderRegistry.from_settings(
        settings(groq_api_key="gsk_test", default_groq_model="llama-3.3-70b-versatile")
    )

    assert registry.get("groq").supported_models() == ["llama-3.3-70b-versatile"]
    # The mock advertises its behaviour models, so failure and cancellation stay
    # reachable from the UI.
    assert "mock-cancel" in registry.get("mock").supported_models()
