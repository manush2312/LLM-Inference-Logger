"""Reads and writes for the inference log -- the system's observability record."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Select, literal, select, tuple_
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
        before_id: uuid.UUID | None = None,
    ) -> list[InferenceLog]:
        """Newest-first listing.

        `before` is a keyset cursor on `started_at`, not an OFFSET. Offset
        pagination re-scans and discards every skipped row, so it degrades
        linearly as a reader pages into a table that this one is designed to
        grow without bound. The cursor rides the existing
        `ix_inference_logs_started_at DESC` index instead.

        `before_id` completes the cursor, and without it pagination silently
        loses rows. The ordering is `(started_at DESC, id DESC)`, so the cursor
        has to be the same pair: filtering on `started_at < before` alone drops
        every row that *shares* the boundary timestamp. Two inferences finishing
        in the same microsecond is not exotic -- concurrent requests do it -- and
        the symptom is the worst kind, a log viewer that is simply missing
        entries with nothing to indicate it.

        Passing `before` alone is still supported for callers that only want a
        time bound rather than a cursor.
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
            if before_id is not None:
                # Row-value comparison, which Postgres can satisfy from the same
                # index the ORDER BY uses. Expressing it as
                # `started_at < b OR (started_at = b AND id < bid)` would be
                # equivalent but harder to read and easier to get backwards.
                # literal() on the right-hand side because `tuple_` builds a SQL
                # row constructor and needs expressions, not bare Python values --
                # mypy catches that, and passing the raw values would otherwise be
                # a plausible-looking runtime surprise.
                stmt = stmt.where(
                    tuple_(InferenceLog.started_at, InferenceLog.id)
                    < tuple_(literal(before), literal(before_id))
                )
            else:
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
