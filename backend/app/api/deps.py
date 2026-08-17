"""FastAPI dependencies.

Every shared resource reaches a route handler through this module. Handlers
never import a global engine or client, which is what lets a test swap in a
fake by overriding one dependency instead of monkey-patching a module.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.repositories.conversations import ConversationRepository
from app.db.repositories.inference_logs import InferenceLogRepository
from app.db.session import Database
from app.events.bus import EventBus
from app.instrumentation.redaction import Redactor
from app.providers.registry import ProviderRegistry
from app.services.chat import StreamingChatService


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One transaction per request.

    The session commits when the handler returns and rolls back if it raises,
    so a handler that fails halfway cannot leave a partial write behind.
    """
    async with get_database(request).session() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_registry(request: Request) -> ProviderRegistry:
    registry: ProviderRegistry = request.app.state.registry
    return registry


RegistryDep = Annotated[ProviderRegistry, Depends(get_registry)]


def get_conversation_repo(session: SessionDep) -> ConversationRepository:
    return ConversationRepository(session)


def get_inference_log_repo(session: SessionDep) -> InferenceLogRepository:
    return InferenceLogRepository(session)


ConversationRepoDep = Annotated[ConversationRepository, Depends(get_conversation_repo)]
InferenceLogRepoDep = Annotated[InferenceLogRepository, Depends(get_inference_log_repo)]


def get_event_bus(request: Request) -> EventBus:
    bus: EventBus = request.app.state.event_bus
    return bus


def get_redactor(request: Request) -> Redactor:
    redactor: Redactor = request.app.state.redactor
    return redactor


def get_chat_service(
    request: Request,
    registry: RegistryDep,
    settings: SettingsDep,
) -> StreamingChatService:
    """Built per request, but from process-wide resources.

    The service opens its own transactions rather than receiving a session,
    because one chat turn spans two of them with a provider call in between --
    see `app.services.chat`.
    """
    return StreamingChatService(
        database=get_database(request),
        registry=registry,
        settings=settings,
        bus=get_event_bus(request),
        redactor=get_redactor(request),
    )


ChatServiceDep = Annotated[StreamingChatService, Depends(get_chat_service)]
