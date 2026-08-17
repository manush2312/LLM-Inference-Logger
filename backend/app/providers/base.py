"""Provider abstraction.

Every provider is normalised to a single method: `stream_chat`, yielding
`StreamChunk`s. That one seam is what the instrumentation wrapper hooks into,
so adding a provider gets full logging for free -- no changes to the wrapper,
the event contract, or the worker.

Two decisions worth stating:

* **Streaming is the only primitive.** Non-streaming is `complete()`, a
  concrete helper that drains the stream. The alternative -- separate
  `generate()` and `stream_chat()` methods -- means two code paths, two places
  to compute usage, and two chances for the instrumentation to diverge. One
  path is one truth.
* **No sampling parameters in the normalised request.** Providers disagree on
  them: `temperature` and `top_p` are rejected outright by current Anthropic
  models but accepted by OpenAI. A shared `temperature` field would therefore
  be a field that silently means "break Anthropic". Each adapter owns whatever
  knobs its own SDK accepts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from app.domain.enums import MessageRole


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One turn of conversation, in provider-neutral form."""

    role: MessageRole
    content: str


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token accounting for a single call.

    Both fields are optional: providers report usage at different points in a
    stream, and a call that fails early may never report it at all. Recording
    `None` is honest; recording `0` would silently understate real spend.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class StreamChunk:
    """One increment of a streaming response.

    A chunk may carry text, metadata, or both. `usage` and `finish_reason`
    typically arrive on the final chunk, but adapters are free to emit them
    whenever their provider does -- the wrapper keeps the last value it sees.
    """

    delta_text: str = ""
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """A normalised request. Deliberately minimal -- see the module docstring."""

    messages: Sequence[ChatMessage]
    model: str
    #: Caps the provider's total output, counting reasoning *and* the visible
    #: answer -- so a value tuned for answer length alone truncates
    #: mid-sentence on a reasoning model.
    #:
    #: This field earns its place in the shared contract only because both
    #: adapters can honour it identically; each still maps it to whichever
    #: parameter its own SDK wants (`max_tokens` for Anthropic,
    #: `max_completion_tokens` for OpenAI, since OpenAI's legacy `max_tokens`
    #: is rejected by reasoning models). Contrast `temperature`, which is
    #: absent precisely because the two providers *cannot* honour it alike.
    max_output_tokens: int = 16_000
    system: str | None = None


@dataclass(frozen=True, slots=True)
class CompletedResponse:
    """The result of draining a stream to completion."""

    text: str
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseProvider(ABC):
    """Adapter from one vendor SDK to this application's vocabulary.

    An adapter's only job is translation: vendor stream events in,
    `StreamChunk`s out, vendor exceptions mapped to `ProviderError`. It does no
    logging, no timing, and no persistence -- that is the wrapper's job, which
    is precisely why the wrapper works for every provider identically.
    """

    #: Registry key. Also the value written to `inference_logs.provider`.
    name: ClassVar[str]

    @abstractmethod
    def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        """Stream a completion. Must raise `ProviderError` on upstream failure."""

    @abstractmethod
    def default_model(self) -> str:
        """Model used when the caller does not name one."""

    def supported_models(self) -> list[str]:
        """Models this provider advertises to the UI.

        Defaults to just the configured default. Providers with a fixed, known
        set (the mock) override it. Deliberately not a live API call: building
        the model picker must not depend on a network round trip to every
        configured vendor at page load.
        """
        return [self.default_model()]

    def supports_model(self, model: str) -> bool:
        """Whether this provider serves `model`.

        Defaults to True: real vendors have open, frequently-changing
        catalogues, so hardcoding a list here would reject valid models the day
        a new one ships. They reject unknown models themselves, and the adapter
        translates that.

        Providers with a genuinely *closed* set override this -- the mock has no
        upstream to complain, so without it a request for someone else's model
        gets served a mock reply and looks like a working answer.
        """
        return True

    def max_output_tokens_cap(self) -> int | None:
        """A hard ceiling this provider imposes, if any.

        Distinct from the request's `max_output_tokens`, which is what the
        *caller* wants. This is what the provider will actually accept, and the
        two genuinely differ: Groq's free tier counts `max_completion_tokens`
        against an 8000 tokens-per-minute budget, so a 16,000-token request is
        rejected with a 413 before generating anything. A single global value
        that is right for a paid Anthropic tier is fatal there.
        """
        return None

    def clamp_output_tokens(self, requested: int) -> int:
        """Reduce a requested budget to what this provider can accept."""
        cap = self.max_output_tokens_cap()
        return min(requested, cap) if cap is not None else requested

    def is_configured(self) -> bool:
        """Whether this provider has what it needs to serve traffic.

        Providers that fail this are omitted from the registry, so an
        unconfigured provider produces a clear "not configured" error at the
        API boundary rather than an authentication failure from deep inside a
        vendor SDK.
        """
        return True

    async def complete(self, request: ChatRequest) -> CompletedResponse:
        """Non-streaming convenience, built on the streaming path.

        Shares the exact code path -- and therefore the exact instrumentation
        -- as streaming, rather than duplicating it.
        """
        parts: list[str] = []
        usage: TokenUsage | None = None
        finish_reason: str | None = None
        metadata: dict[str, Any] = {}

        async for chunk in self.stream_chat(request):
            parts.append(chunk.delta_text)
            if chunk.usage is not None:
                usage = chunk.usage
            if chunk.finish_reason is not None:
                finish_reason = chunk.finish_reason
            metadata.update(chunk.metadata)

        return CompletedResponse(
            text="".join(parts),
            usage=usage,
            finish_reason=finish_reason,
            metadata=metadata,
        )
