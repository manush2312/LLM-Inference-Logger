"""Anthropic adapter.

Translates the Anthropic streaming API into `StreamChunk`s. Nothing about
logging, timing or persistence appears here -- that is the wrapper's job.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar, Literal

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types import MessageParam

from app.core.errors import ProviderError
from app.domain.enums import MessageRole
from app.providers.base import BaseProvider, ChatRequest, StreamChunk, TokenUsage


class AnthropicProvider(BaseProvider):
    name: ClassVar[str] = "anthropic"

    def __init__(self, api_key: str | None, default_model: str) -> None:
        self._api_key = api_key
        self._default_model = default_model
        self._client = AsyncAnthropic(api_key=api_key) if api_key else None

    def is_configured(self) -> bool:
        return self._client is not None

    def default_model(self) -> str:
        return self._default_model

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        if self._client is None:  # pragma: no cover -- registry filters these out
            raise ProviderError("Anthropic API key is not configured", provider=self.name)

        system, messages = self._split_system(request)

        try:
            async with self._client.messages.stream(
                model=request.model,
                max_tokens=request.max_output_tokens,
                # Sampling parameters are deliberately absent: current
                # Anthropic models reject `temperature` and `top_p` outright.
                system=system if system is not None else anthropic.omit,
                messages=messages,
            ) as stream:
                # Iterating raw events rather than the `text_stream` helper:
                # usage arrives on `message_start` and `message_delta`, and the
                # text-only helper discards both. Reading events keeps token
                # accounting on the same pass as the text.
                async for event in stream:
                    chunk = self._to_chunk(event)
                    if chunk is not None:
                        yield chunk
        except anthropic.APIError as exc:
            raise self._translate(exc) from exc

    @staticmethod
    def _to_chunk(event: Any) -> StreamChunk | None:
        """Map one Anthropic stream event to a chunk, or None if uninteresting."""
        match event.type:
            case "content_block_delta" if event.delta.type == "text_delta":
                return StreamChunk(delta_text=event.delta.text)

            case "message_start":
                usage = event.message.usage
                return StreamChunk(
                    usage=TokenUsage(input_tokens=usage.input_tokens),
                    metadata={"provider_request_id": event.message.id},
                )

            case "message_delta":
                # Output tokens are only final here; the input count from
                # `message_start` is merged by the wrapper, which keeps the
                # last non-null value for each field.
                return StreamChunk(
                    finish_reason=event.delta.stop_reason,
                    usage=TokenUsage(output_tokens=event.usage.output_tokens),
                )

            case _:
                return None

    @staticmethod
    def _split_system(request: ChatRequest) -> tuple[str | None, list[MessageParam]]:
        """Anthropic takes the system prompt as a top-level argument.

        System turns embedded in the message list would be rejected, so they
        are hoisted here rather than pushed onto every caller.
        """
        system_parts = [request.system] if request.system else []
        turns: list[MessageParam] = []

        for message in request.messages:
            if message.role is MessageRole.SYSTEM:
                system_parts.append(message.content)
                continue

            role: Literal["user", "assistant"] = (
                "assistant" if message.role is MessageRole.ASSISTANT else "user"
            )
            turns.append({"role": role, "content": message.content})

        return ("\n\n".join(system_parts) or None), turns

    def _translate(self, exc: anthropic.APIError) -> ProviderError:
        """Map vendor exceptions onto the app's taxonomy.

        Retryability is decided here, once, from the SDK's own typed exception
        classes rather than by string-matching error messages downstream.
        """
        retryable = isinstance(
            exc,
            anthropic.RateLimitError | anthropic.InternalServerError | anthropic.APIConnectionError,
        )
        return ProviderError(
            str(exc),
            provider=self.name,
            retryable=retryable,
            error_type=type(exc).__name__,
        )
