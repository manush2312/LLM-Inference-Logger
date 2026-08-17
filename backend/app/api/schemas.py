"""HTTP request and response shapes.

Deliberately separate from the ORM models. A wire contract and a storage schema
change for different reasons and at different rates; coupling them means every
column rename is a breaking API change, and every field the database needs
becomes a field the client can see.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import ConversationStatus, MessageRole

#: Bounds the request body. Large enough for a genuine prompt, small enough
#: that a runaway client cannot push an unbounded string into the database.
MAX_MESSAGE_CHARS = 32_000


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: MessageRole
    content: str
    seq: int
    created_at: datetime


class ConversationSummary(BaseModel):
    """List-view shape -- no messages, so the sidebar never loads transcripts."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    status: ConversationStatus
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationSummary):
    messages: list[MessageRead]


class ConversationList(BaseModel):
    items: list[ConversationSummary]
    total: int


class ChatRequestBody(BaseModel):
    #: Omit to start a new conversation. Supplying it appends to an existing
    #: one, which is what makes a refreshed browser able to resume.
    conversation_id: uuid.UUID | None = None
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    provider: str | None = None
    model: str | None = None


class ChatResponse(BaseModel):
    conversation_id: uuid.UUID
    user_message: MessageRead
    assistant_message: MessageRead
    provider: str
    model: str


class ProviderInfo(BaseModel):
    name: str
    default_model: str
    is_default: bool


class ProviderList(BaseModel):
    """Lets the UI populate its picker from the server's real configuration.

    Without this the frontend would hardcode a provider list and offer options
    that 400 on selection.
    """

    items: list[ProviderInfo]
    default: str
