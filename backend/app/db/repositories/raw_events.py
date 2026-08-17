"""The raw-event landing zone.

Every event the worker receives is written here verbatim before any parsing is
attempted. If the parser rejects it, the payload is still on disk with the
reason attached -- so a schema bug becomes a replay, not a permanent hole in
the data.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import RawEvent
from app.db.repositories.base import Repository
from app.domain.enums import ProcessingStatus


class RawEventRepository(Repository):
    async def record(
        self, *, event_id: uuid.UUID, event_type: str, payload: dict[str, Any]
    ) -> bool:
        """Land a raw payload. Idempotent on redelivery, like the parsed row."""
        stmt = (
            pg_insert(RawEvent)
            .values(
                id=event_id,
                event_type=event_type,
                payload=payload,
                processing_status=ProcessingStatus.PENDING,
            )
            .on_conflict_do_nothing(index_elements=[RawEvent.id])
        )
        return await self._affected_rows(stmt) > 0

    async def mark_processed(self, event_id: uuid.UUID) -> None:
        await self._session.execute(
            update(RawEvent)
            .where(RawEvent.id == event_id)
            .values(
                processing_status=ProcessingStatus.PROCESSED,
                processed_at=func.now(),
                processing_error=None,
            )
        )

    async def mark_failed(self, event_id: uuid.UUID, *, error: str) -> None:
        await self._session.execute(
            update(RawEvent)
            .where(RawEvent.id == event_id)
            .values(
                processing_status=ProcessingStatus.FAILED,
                processed_at=func.now(),
                # Bound the stored text: a pathological validation error should
                # not write a megabyte of prose into every failed row.
                processing_error=error[:4000],
            )
        )

    async def list_failed(self, *, limit: int = 100) -> list[RawEvent]:
        """Triage query for the failed events -- and the input to a replay."""
        stmt = (
            select(RawEvent)
            .where(RawEvent.processing_status == ProcessingStatus.FAILED)
            .order_by(RawEvent.received_at.desc())
            .limit(limit)
        )
        return list((await self._session.scalars(stmt)).all())
