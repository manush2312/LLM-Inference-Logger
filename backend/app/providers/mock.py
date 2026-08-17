"""A provider that needs no API key, no network, and no money.

This exists so the entire system -- streaming, latency, TTFT, token accounting,
cancellation, error logging, the dashboards -- is demonstrable and testable by
anyone who clones the repo, with `DEFAULT_PROVIDER=mock` and nothing else
configured. It is a real implementation of `BaseProvider`, not a test double:
the wrapper, the event bus, the worker and the database cannot tell it apart
from Anthropic.

It is also the only way to exercise failure paths deterministically. You cannot
ask a real provider to fail on demand, so without this the error and
cancellation dashboards would be untested and demoed with empty tables.

Behaviour is selected by model name, so every path is reachable from the UI:

* `mock`         -- ordinary streamed response
* `mock-slow`    -- long pauses between tokens; use it to demo cancellation
* `mock-error`   -- fails partway through, after emitting some output
* `mock-instant` -- no delays at all; used by the test suite
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from typing import ClassVar, Final

from app.core.errors import ProviderError
from app.domain.enums import MessageRole
from app.providers.base import BaseProvider, ChatRequest, StreamChunk, TokenUsage

#: Per-token delay for each behaviour, in seconds.
_DELAYS: Final[dict[str, float]] = {
    "mock": 0.04,
    "mock-slow": 0.6,
    "mock-error": 0.04,
    "mock-instant": 0.0,
}

_ERROR_MODEL: Final = "mock-error"

_TEMPLATE: Final = (
    "You asked about {topic}. Here is a deterministic reply from the mock "
    "provider, which streams token by token so that latency, time-to-first-token "
    "and cancellation all behave exactly as they would against a real model. "
    "Nothing here reaches the network."
)


class MockProvider(BaseProvider):
    name: ClassVar[str] = "mock"

    def default_model(self) -> str:
        return "mock"

    def supported_models(self) -> list[str]:
        return list(_DELAYS)

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        delay = _DELAYS.get(request.model, _DELAYS["mock"])
        words = _TEMPLATE.format(topic=self._topic(request)).split()

        # Token counts are derived from the text rather than invented, so the
        # dashboard's cost panels show internally consistent numbers.
        input_tokens = sum(len(m.content.split()) for m in request.messages)

        for index, word in enumerate(words):
            # `mock-error` fails *after* streaming some output, which is the
            # interesting case: it produces a log row with a partial
            # `output_preview` and a non-null TTFT alongside its error.
            if request.model == _ERROR_MODEL and index == len(words) // 3:
                raise ProviderError(
                    "Simulated upstream failure from the mock provider",
                    provider=self.name,
                    retryable=True,
                )

            if delay:
                await asyncio.sleep(delay)

            yield StreamChunk(delta_text=word + " ")

        yield StreamChunk(
            finish_reason="stop",
            usage=TokenUsage(input_tokens=input_tokens, output_tokens=len(words)),
            # Mirrors the vendor-specific extras a real adapter records, so the
            # `raw_metadata` JSONB column is exercised by the mock path too.
            metadata={"provider_request_id": self._request_id(request)},
        )

    @staticmethod
    def _topic(request: ChatRequest) -> str:
        for message in reversed(request.messages):
            if message.role is MessageRole.USER:
                return message.content.strip()[:80] or "nothing in particular"
        return "nothing in particular"

    @staticmethod
    def _request_id(request: ChatRequest) -> str:
        """Stable pseudo-id, so repeated runs of a demo produce stable output."""
        seed = "|".join(m.content for m in request.messages)
        return "mock_" + hashlib.sha256(seed.encode()).hexdigest()[:16]
