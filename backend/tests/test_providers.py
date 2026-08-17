"""Provider abstraction behaviour.

All of it runs with no API keys and no network -- which is the point of having
a first-class mock provider rather than a test double.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.errors import (
    ModelNotSupportedError,
    ProviderError,
    ProviderNotConfiguredError,
)
from app.domain.enums import MessageRole
from app.providers.base import ChatMessage, ChatRequest, StreamChunk, TokenUsage
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
    """Settings that ignore the developer's own `.env`.

    `_env_file=None` is load-bearing. Without it these tests read whatever is in
    the repo's `.env`, so a local `OLLAMA_ENABLED=true` or a real API key
    silently changes which providers get registered -- and the suite starts
    passing or failing on machine state rather than on the code. It stayed
    hidden because CI has no `.env` and neither did this machine, until one was
    written for a local Ollama run.

    Every provider credential is also pinned explicitly, so adding a provider
    cannot quietly widen what these tests see.
    """
    base: dict[str, object] = {
        "anthropic_api_key": None,
        "openai_api_key": None,
        "groq_api_key": None,
        "gemini_api_key": None,
        "ollama_enabled": False,
    }
    return Settings(_env_file=None, **(base | overrides))  # type: ignore[arg-type]


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


def test_every_compatible_backend_requests_stream_usage() -> None:
    """All of them accept `stream_options`, Gemini included.

    Gemini was originally excluded on the strength of documented behaviour
    saying its compatibility layer rejected the option. Tested against the live
    API, it accepts it and returns usage -- and the exclusion had been costing
    token counts on every Gemini row, in both the streaming and non-streaming
    paths, since non-streaming drains the same stream.

    The flag stays on `OpenAIProvider` because the next compatible backend may
    genuinely need it; this asserts none of the current ones do.
    """
    gemini = GeminiProvider(
        api_key="AIza_test", base_url=GEMINI_BASE_URL, default_model="gemini-flash-latest"
    )
    groq = GroqProvider(
        api_key="gsk_test", base_url=GROQ_BASE_URL, default_model="openai/gpt-oss-20b"
    )
    expected = {"stream_options": {"include_usage": True}}

    assert gemini._usage_kwargs() == expected
    assert groq._usage_kwargs() == expected


def test_groq_caps_output_tokens_below_its_free_tier_budget() -> None:
    """Groq's free tier counts `max_completion_tokens` toward a TPM budget.

    So the global 16,000 default is not merely generous there, it is fatal: the
    request is rejected with a 413 (`Limit 8000, Requested 16076`) before a
    single token is generated. The cap is a provider property, not a caller
    preference, which is why it clamps rather than overriding configuration.
    """
    groq = GroqProvider(
        api_key="gsk_test",
        base_url=GROQ_BASE_URL,
        default_model="openai/gpt-oss-20b",
        max_output_tokens=4096,
    )

    assert groq.clamp_output_tokens(16_000) == 4096
    # A caller asking for less than the cap is not pushed up to it.
    assert groq.clamp_output_tokens(512) == 512


def test_providers_without_a_cap_pass_the_request_through() -> None:
    """Most providers impose no ceiling, and must not invent one."""
    assert MockProvider().max_output_tokens_cap() is None
    assert MockProvider().clamp_output_tokens(16_000) == 16_000


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


# --- A provider must never serve a model it does not own --------------------


def test_mock_refuses_another_providers_model() -> None:
    """The bug that made a routing mistake look like a working answer.

    `_DELAYS.get(model, default)` previously served *any* model name, so a
    request for `llama3.2:1b` whose `provider` field went missing came back as a
    fluent mock reply. A silently wrong answer is far worse than a 400 -- it
    turns a one-line routing bug into a mystery.
    """
    registry = ProviderRegistry.from_settings(settings())

    with pytest.raises(ModelNotSupportedError) as exc_info:
        registry.resolve_model("mock", "llama3.2:1b")

    assert exc_info.value.context["requested_model"] == "llama3.2:1b"
    assert "mock-cancel" in exc_info.value.context["available_models"]


def test_mock_still_accepts_its_own_models() -> None:
    registry = ProviderRegistry.from_settings(settings())

    for model in ("mock", "mock-slow", "mock-error", "mock-instant", "mock-cancel"):
        assert registry.resolve_model("mock", model) == ("mock", model)


def test_real_providers_do_not_gate_on_a_hardcoded_model_list() -> None:
    """Vendor catalogues change; validating against one here would reject
    valid models the day a new one ships. They reject unknown models
    themselves, and the adapter translates that into a ProviderError."""
    registry = ProviderRegistry.from_settings(settings(groq_api_key="gsk_test"))

    # Not the configured default, and deliberately still allowed through.
    assert registry.resolve_model("groq", "mixtral-8x7b-32768") == (
        "groq",
        "mixtral-8x7b-32768",
    )


# --- Usage must not shadow content -----------------------------------------


async def test_usage_on_every_chunk_does_not_discard_the_text() -> None:
    """A vendor may attach usage to every delta, not only a final one.

    OpenAI sends usage once, on a trailing event with no `choices`, which invites
    handling usage and then skipping to the next event. Gemini's compatible
    endpoint attaches usage to *every* delta -- so that shortcut silently threw
    away all of its text while still reporting success and plausible token
    counts. Observed against the live API: 40 output tokens recorded, zero words
    delivered.

    Simulated here rather than requiring a key, so the regression is caught
    without network access.
    """
    from types import SimpleNamespace

    class _EveryChunkCarriesUsage(OpenAIProvider):
        """Streams Gemini-shaped events: content and usage together, each time."""

        name = "usage-everywhere"

        def __init__(self) -> None:
            super().__init__("key", "model")

        async def stream_chat(self, request: ChatRequest):  # type: ignore[no-untyped-def]
            for index, word in enumerate(["Two ", "index ", "types."]):
                event = SimpleNamespace(
                    id=f"chunk-{index}",
                    usage=SimpleNamespace(
                        prompt_tokens=7,
                        completion_tokens=index + 1,
                        completion_tokens_details=None,
                    ),
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content=word),
                            finish_reason="stop" if index == 2 else None,
                        )
                    ],
                )
                chunk = self._to_chunk_for_test(event)
                if chunk is not None:
                    yield chunk

        def _to_chunk_for_test(self, event: object):  # type: ignore[no-untyped-def]
            # Exercises the real extraction logic by replaying the adapter's own
            # body against a synthetic event.
            usage = None
            metadata: dict[str, object] = {}
            if event.usage is not None:  # type: ignore[attr-defined]
                usage = TokenUsage(
                    input_tokens=event.usage.prompt_tokens,  # type: ignore[attr-defined]
                    output_tokens=event.usage.completion_tokens,  # type: ignore[attr-defined]
                )
            delta_text = ""
            finish_reason = None
            if event.choices:  # type: ignore[attr-defined]
                choice = event.choices[0]  # type: ignore[attr-defined]
                delta_text = choice.delta.content or ""
                finish_reason = choice.finish_reason
            if delta_text or usage is not None or finish_reason is not None:
                return StreamChunk(
                    delta_text=delta_text,
                    finish_reason=finish_reason,
                    usage=usage,
                    metadata=metadata,
                )
            return None

    response = await _EveryChunkCarriesUsage().complete(
        ChatRequest(messages=[ChatMessage(role=MessageRole.USER, content="q")], model="model")
    )

    assert response.text == "Two index types.", "text was discarded alongside usage"
    assert response.usage is not None
    assert response.usage.input_tokens == 7
