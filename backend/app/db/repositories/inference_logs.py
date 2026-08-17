"""Reads and writes for the inference log -- the system's observability record."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import InferenceLog
from app.db.repositories.base import Repository
from app.domain.enums import InferenceStatus
from app.domain.events import InferenceEvent


class InferenceLogRepository(Repository):
    async def upsert(self, event: InferenceEvent) -> bool:
        """Persist an event idempotently. Returns True if a row was inserted.

        `ON CONFLICT DO NOTHING` is the whole reason ingestion can be
        at-least-once. Redis consumer groups redeliver whenever a message is
        not acked in time -- after a worker crash, a slow database, a rolling
        restart -- and a plain INSERT would turn each of those into a duplicate
        row that quietly skews every count and percentile.

        A False return is the normal, healthy signal that a redelivery was
        correctly ignored; it is not an error.
        """
        stmt = (
            pg_insert(InferenceLog)
            .values(**event.to_row())
            .on_conflict_do_nothing(index_elements=[InferenceLog.id])
        )
        return await self._affected_rows(stmt) > 0

    async def get(self, log_id: uuid.UUID) -> InferenceLog | None:
        return await self._session.get(InferenceLog, log_id)

    async def list_recent(
        self,
        *,
        limit: int = 50,
        provider: str | None = None,
        model: str | None = None,
        status: InferenceStatus | None = None,
        conversation_id: uuid.UUID | None = None,
        since: datetime | None = None,
        before: datetime | None = None,
    ) -> list[InferenceLog]:
        """Newest-first listing.

        `before` is a keyset cursor on `started_at`, not an OFFSET. Offset
        pagination re-scans and discards every skipped row, so it degrades
        linearly as a reader pages into a table that this one is designed to
        grow without bound. The cursor rides the existing
        `ix_inference_logs_started_at DESC` index instead.
        """
        stmt: Select[tuple[InferenceLog]] = select(InferenceLog)

        if provider is not None:
            stmt = stmt.where(InferenceLog.provider == provider)
        if model is not None:
            stmt = stmt.where(InferenceLog.model == model)
        if status is not None:
            stmt = stmt.where(InferenceLog.status == status)
        if conversation_id is not None:
            stmt = stmt.where(InferenceLog.conversation_id == conversation_id)
        if since is not None:
            stmt = stmt.where(InferenceLog.started_at >= since)
        if before is not None:
            stmt = stmt.where(InferenceLog.started_at < before)

        stmt = stmt.order_by(InferenceLog.started_at.desc(), InferenceLog.id.desc()).limit(limit)
        return list((await self._session.scalars(stmt)).all())

    async def list_for_conversation(self, conversation_id: uuid.UUID) -> list[InferenceLog]:
        stmt = (
            select(InferenceLog)
            .where(InferenceLog.conversation_id == conversation_id)
            .order_by(InferenceLog.started_at.asc())
        )
        return list((await self._session.scalars(stmt)).all())
