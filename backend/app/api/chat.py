"""Chat endpoint.

Non-streaming for now. It becomes SSE in M4 without changing the service
beneath it, because the provider layer already streams -- this endpoint just
chooses to wait for the whole response.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ChatServiceDep, RegistryDep
from app.api.schemas import (
    ChatRequestBody,
    ChatResponse,
    MessageRead,
    ProviderInfo,
    ProviderList,
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
