"""Chat endpoint.

Non-streaming for now. It becomes SSE in M4 without changing the service
beneath it, because the provider layer already streams -- this endpoint just
chooses to wait for the whole response.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.deps import ChatServiceDep, RegistryDep
from app.api.schemas import (
    ChatRequestBody,
    ChatResponse,
    MessageRead,
    ProviderInfo,
    ProviderList,
)
from app.api.sse import format_sse, stream_with_disconnect_cancellation
from app.services.chat import (
    ChatStreamEvent,
    StreamCompleted,
    StreamDelta,
    StreamFailed,
    StreamFirstToken,
    StreamStarted,
)

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequestBody, service: ChatServiceDep) -> ChatResponse:
    result = await service.send(
        content=body.content,
        conversation_id=body.conversation_id,
        provider=body.provider,
        model=body.model,
    )

    return ChatResponse(
        conversation_id=result.conversation.id,
        user_message=MessageRead.model_validate(result.user_message),
        assistant_message=MessageRead.model_validate(result.assistant_message),
        provider=result.provider,
        model=result.model,
    )


def _to_sse(event: ChatStreamEvent) -> str:
    """Map one domain event onto one SSE frame.

    Exhaustive by construction: `ChatStreamEvent` is a closed union, so adding
    a variant without handling it here is a type error rather than a silently
    dropped frame.
    """
    match event:
        case StreamStarted():
            return format_sse(
                "start",
                {
                    "conversation_id": str(event.conversation_id),
                    "provider": event.provider,
                    "model": event.model,
                },
            )
        case StreamFirstToken():
            # Metadata, deliberately not folded into the first content frame.
            return format_sse("ttft", {"ttft_ms": event.ttft_ms})
        case StreamDelta():
            return format_sse("chunk", {"text": event.text})
        case StreamCompleted():
            return format_sse(
                "done",
                {"message_id": str(event.message_id), "latency_ms": event.latency_ms},
            )
        case StreamFailed():
            return format_sse("error", {"code": event.code, "message": event.message})


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequestBody, request: Request, service: ChatServiceDep
) -> StreamingResponse:
    """Stream a reply as Server-Sent Events.

    Note the status code is always 200: by the time anything can fail, the
    headers are already on the wire. Failures arrive as an `error` event
    instead -- see `StreamFailed`.
    """

    async def frames() -> AsyncIterator[str]:
        async for event in service.stream(
            content=body.content,
            conversation_id=body.conversation_id,
            provider=body.provider,
            model=body.model,
        ):
            yield _to_sse(event)

    return StreamingResponse(
        stream_with_disconnect_cancellation(request, frames()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Nginx buffers proxied responses by default, which would hold the
            # whole stream back and deliver it in one lump at the end --
            # defeating streaming entirely behind a standard ingress.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/providers", response_model=ProviderList, tags=["providers"])
async def list_providers(registry: RegistryDep) -> ProviderList:
    """What this deployment can actually serve.

    The UI builds its picker from this rather than a hardcoded list, so it can
    never offer a provider that will 400 when selected.
    """
    return ProviderList(
        items=[
            ProviderInfo(
                name=name,
                default_model=registry.get(name).default_model(),
                is_default=name == registry.default_name,
            )
            for name in registry.available()
        ],
        default=registry.default_name,
    )
