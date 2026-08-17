"""Providers that speak the OpenAI wire format at a different address.

Groq, Google Gemini (via its OpenAI-compatibility endpoint) and Ollama all
expose `/v1/chat/completions` with OpenAI's request and response shapes. So none
of them needs a new adapter -- they need the existing one pointed somewhere
else, which is what `OpenAICompatibleProvider` does.

Two decisions worth naming:

* **Each gets its own registry name**, rather than all masquerading as
  `openai`. `inference_logs.provider` is what the dashboard groups by, so
  sharing a name would make Groq and OpenAI traffic indistinguishable in every
  panel -- and this is a multi-provider observability tool, so that is the one
  thing it must not lose.
* **Subclassing rather than editing the OpenAI adapter.** All the stream
  translation, error mapping and usage merging is inherited unchanged; a
  subclass supplies only an address, a name and a default model. Adding the
  next compatible vendor is about six lines.

Compatibility is close but not exact -- the `requests_stream_usage` flag exists
because some compatibility layers reject `stream_options`. That is a real
difference between vendors, not a detail to paper over: getting it wrong means
either a hard 400 or silently null token counts.
"""

from __future__ import annotations

from typing import ClassVar, Final

from openai import AsyncOpenAI

from app.providers.openai_provider import OpenAIProvider

#: Vendor endpoints. Each already ends in the OpenAI-style version segment,
#: because the SDK appends only `/chat/completions` to whatever it is given.
GROQ_BASE_URL: Final = "https://api.groq.com/openai/v1"
GEMINI_BASE_URL: Final = "https://generativelanguage.googleapis.com/v1beta/openai/"
OLLAMA_BASE_URL: Final = "http://localhost:11434/v1"


class OpenAICompatibleProvider(OpenAIProvider):
    """An OpenAI-shaped API hosted somewhere other than OpenAI."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        default_model: str,
        max_output_tokens: int | None = None,
    ) -> None:
        # Deliberately not calling super().__init__: it builds a client pointed
        # at OpenAI. Everything else about the parent is reused as-is.
        self._default_model = default_model
        self._max_output_tokens = max_output_tokens
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url) if api_key else None

    def max_output_tokens_cap(self) -> int | None:
        return self._max_output_tokens


class GroqProvider(OpenAICompatibleProvider):
    """Groq. Free tier, no card required, and very fast.

    Runs open models on custom silicon. Streaming usage reporting is supported,
    so token accounting works exactly as it does against OpenAI.

    Its free tier counts `max_completion_tokens` toward a tokens-per-minute
    budget, so it needs an output cap well below the global default -- see
    `max_output_tokens_cap`. Observed as a 413 `rate_limit_exceeded`:
    "Limit 8000, Requested 16076".
    """

    name: ClassVar[str] = "groq"


class GeminiProvider(OpenAICompatibleProvider):
    """Google Gemini through its OpenAI-compatibility endpoint.

    Google also ships a native SDK (`google-genai`). This path is used instead
    because it needs no extra dependency and no new adapter -- the tradeoff is
    that Gemini-specific features (safety settings, native function-calling
    shapes) are not reachable through it. Worth swapping to the native SDK only
    if those are needed; for chat, streaming and usage it is equivalent.
    """

    name: ClassVar[str] = "gemini"

    # No `requests_stream_usage` override. It was set to False here on the
    # strength of documented behaviour saying the compatibility layer rejected
    # `stream_options` -- then tested against the live API, which accepts it and
    # returns usage. Leaving the override in place cost token counts on every
    # Gemini row, in both the streaming and non-streaming paths (non-streaming
    # drains the same stream), for no reason at all.


class OllamaProvider(OpenAICompatibleProvider):
    """A model running on this machine. No key, no network, no cost.

    Ollama ignores the API key entirely, but the OpenAI client refuses to
    construct without one, so a placeholder is passed. That is why this class
    exists rather than reusing the base directly -- "needs no credential" is a
    real difference in configuration semantics, not just a different URL.
    """

    name: ClassVar[str] = "ollama"

    def __init__(self, *, base_url: str, default_model: str) -> None:
        super().__init__(
            # Any non-empty string. Ollama does not check it; the SDK only
            # requires it to be present.
            api_key="ollama",
            base_url=base_url,
            default_model=default_model,
        )
