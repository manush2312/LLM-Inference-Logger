"""Conversation CRUD.

Thin by design: every handler translates JSON to repository arguments and back.
Business rules live in the repository and the service, so they are testable
without HTTP.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Response, status

from app.api.deps import ConversationRepoDep
from app.api.schemas import (
    ConversationDetail,
    ConversationList,
    ConversationSummary,
    MessageRead,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post("", response_model=ConversationSummary, status_code=status.HTTP_201_CREATED)
async def create_conversation(repo: ConversationRepoDep) -> ConversationSummary:
    """Start an empty conversation.

    Optional in practice -- `POST /chat` with no `conversation_id` creates one
    implicitly -- but it lets a client reserve an id before the first message.
    """
    conversation = await repo.create()
    return ConversationSummary.model_validate(conversation)


@router.get("", response_model=ConversationList)
async def list_conversations(
    repo: ConversationRepoDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ConversationList:
    conversations = await repo.list_recent(limit=limit, offset=offset)
    return ConversationList(
        items=[ConversationSummary.model_validate(c) for c in conversations],
        total=await repo.count(),
    )


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID, repo: ConversationRepoDep
) -> ConversationDetail:
    """Full transcript in `seq` order -- this is what resumes a refreshed page."""
    conversation = await repo.require(conversation_id)
    messages = await repo.messages(conversation_id)

    return ConversationDetail(
        **ConversationSummary.model_validate(conversation).model_dump(),
        messages=[MessageRead.model_validate(m) for m in messages],
    )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation_id: uuid.UUID, repo: ConversationRepoDep) -> Response:
    """Deletes the transcript. Inference logs survive -- see `models.py`."""
    await repo.delete(conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
