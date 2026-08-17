"""OpenAI adapter.

Same contract as the Anthropic adapter, so the instrumentation wrapper treats
them identically -- which is the whole point of the provider abstraction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar

import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from openai.types.completion_usage import CompletionUsage

from app.core.errors import ProviderError
from app.domain.enums import MessageRole
from app.providers.base import BaseProvider, ChatRequest, StreamChunk, TokenUsage


class OpenAIProvider(BaseProvider):
    name: ClassVar[str] = "openai"

    def __init__(self, api_key: str | None, default_model: str) -> None:
        self._default_model = default_model
        self._client = AsyncOpenAI(api_key=api_key) if api_key else None

    def is_configured(self) -> bool:
        return self._client is not None

    def default_model(self) -> str:
        return self._default_model

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        if self._client is None:  # pragma: no cover -- registry filters these out
            raise ProviderError("OpenAI API key is not configured", provider=self.name)

        try:
            stream = await self._client.chat.completions.create(
                model=request.model,
                messages=self._to_openai_messages(request),
                # `max_completion_tokens`, not the legacy `max_tokens`: the
                # latter is rejected outright by every reasoning model
                # (o-series, gpt-5*). This one is also the true analogue of
                # Anthropic's budget -- it covers reasoning plus visible
                # output, which is what makes one normalised
                # `max_output_tokens` mean the same thing on both providers.
                max_completion_tokens=request.max_output_tokens,
                stream=True,
                # Without this, a streaming response reports no usage at all
                # and every OpenAI row would land with null token counts --
                # silently breaking the cost panel for one provider only.
                stream_options={"include_usage": True},
            )

            async for event in stream:
                # The final usage-bearing event carries an empty `choices`
                # list, so this must not assume a choice is present.
                if event.usage is not None:
                    yield StreamChunk(
                        usage=TokenUsage(
                            input_tokens=event.usage.prompt_tokens,
                            # Includes reasoning tokens on reasoning models --
                            # same convention as Anthropic, where thinking
                            # counts toward output. Billed spend, not visible
                            # length.
                            output_tokens=event.usage.completion_tokens,
                        ),
                        metadata=self._usage_metadata(event.id, event.usage),
                    )
                    continue

                if not event.choices:
                    continue

                choice = event.choices[0]
                yield StreamChunk(
                    delta_text=choice.delta.content or "",
                    finish_reason=choice.finish_reason,
                )
        except openai.APIError as exc:
            raise self._translate(exc) from exc

    @staticmethod
    def _usage_metadata(request_id: str, usage: CompletionUsage) -> dict[str, Any]:
        """Preserve the reasoning/visible split that `output_tokens` flattens.

        `completion_tokens` bundles reasoning tokens together with the text the
        user actually sees. Recording only that number makes it impossible to
        answer "how much of our spend was reasoning nobody read?" -- so the
        breakdown goes into `raw_metadata`, which exists precisely for
        provider-specific detail that is worth keeping but not worth a column.
        """
        metadata: dict[str, Any] = {"provider_request_id": request_id}

        details = usage.completion_tokens_details
        if details is not None and details.reasoning_tokens is not None:
            metadata["reasoning_tokens"] = details.reasoning_tokens

        return metadata

    @staticmethod
    def _to_openai_messages(request: ChatRequest) -> list[ChatCompletionMessageParam]:
        """Unlike Anthropic, OpenAI carries the system prompt inside the list."""
        messages: list[ChatCompletionMessageParam] = []

        if request.system:
            messages.append({"role": "system", "content": request.system})

        for message in request.messages:
            match message.role:
                case MessageRole.SYSTEM:
                    messages.append({"role": "system", "content": message.content})
                case MessageRole.ASSISTANT:
                    messages.append({"role": "assistant", "content": message.content})
                case MessageRole.USER:
                    messages.append({"role": "user", "content": message.content})

        return messages

    def _translate(self, exc: openai.APIError) -> ProviderError:
        retryable = isinstance(
            exc,
            openai.RateLimitError | openai.InternalServerError | openai.APIConnectionError,
        )
        return ProviderError(
            str(exc),
            provider=self.name,
            retryable=retryable,
            error_type=type(exc).__name__,
        )
