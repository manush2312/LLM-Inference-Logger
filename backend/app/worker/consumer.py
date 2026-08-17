"""Ingestion worker.

Reads events off the stream and lands them in Postgres. Runs as its own
process so that write-heavy ingestion never competes for capacity with the
latency-sensitive chat path -- which is also why they scale independently in
the Kubernetes manifests.

Ordering inside one unit of work is deliberate:

1. Land the raw payload in `events_raw` -- *before* parsing, so a payload that
   the schema rejects is still on disk with the reason attached.
2. Parse and upsert into `inference_logs`.
3. Mark the raw row processed.
4. Only then acknowledge.

Acknowledging last is what makes this at-least-once rather than at-most-once:
if the process dies between the write and the ack, Redis redelivers, and the
idempotent upsert absorbs the duplicate. The reverse order would silently lose
events on every crash.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.core.logging import get_logger
from app.db.repositories.inference_logs import InferenceLogRepository
from app.db.repositories.raw_events import RawEventRepository
from app.db.session import Database
from app.domain.events import EVENT_TYPE_INFERENCE, InferenceEvent
from app.events.bus import DeliveredEvent, EventStream

log = get_logger(__name__)


@dataclass(slots=True)
class BatchOutcome:
    """Per-batch counters, so a run's health is visible in the logs."""

    received: int = 0
    inserted: int = 0
    duplicates: int = 0
    rejected: int = 0


class IngestionWorker:
    def __init__(
        self,
        *,
        stream: EventStream,
        database: Database,
        batch_size: int = 100,
        block_ms: int = 2000,
    ) -> None:
        self._stream = stream
        self._database = database
        self._batch_size = batch_size
        self._block_ms = block_ms
        self._stopping = asyncio.Event()

    def request_stop(self) -> None:
        """Ask the loop to finish the current batch and exit."""
        self._stopping.set()

    async def run_forever(self) -> None:
        await self._stream.ensure_group()
        log.info("worker_started", batch_size=self._batch_size)

        while not self._stopping.is_set():
            try:
                await self.process_batch()
            except Exception as exc:
                # Typically Postgres being unreachable. The batch was not
                # acked, so Redis will redeliver it; back off rather than
                # spinning the CPU against a database that is still down.
                log.exception("worker_batch_failed", error=str(exc))
                await asyncio.sleep(1.0)

        log.info("worker_stopped")

    async def process_batch(self) -> BatchOutcome:
        outcome = BatchOutcome()

        delivered = await self._stream.poll(count=self._batch_size, block_ms=self._block_ms)
        outcome.received = len(delivered)

        for item in delivered:
            await self._handle(item, outcome)

        if outcome.received:
            log.info(
                "batch_processed",
                received=outcome.received,
                inserted=outcome.inserted,
                duplicates=outcome.duplicates,
                rejected=outcome.rejected,
            )

        return outcome

    async def _handle(self, item: DeliveredEvent, outcome: BatchOutcome) -> None:
        event_id = _extract_id(item.payload)

        if event_id is None:
            # No usable id means no way to make the write idempotent, so this
            # cannot safely enter the pipeline at all.
            await self._stream.dead_letter(item.payload, reason="missing_or_invalid_id")
            await self._stream.ack(item.delivery_id)
            outcome.rejected += 1
            return

        async with self._database.session() as session:
            raw_events = RawEventRepository(session)
            logs = InferenceLogRepository(session)

            await raw_events.record(
                event_id=event_id,
                event_type=str(item.payload.get("event_type", EVENT_TYPE_INFERENCE)),
                payload=item.payload,
            )

            try:
                event = InferenceEvent.model_validate(item.payload)
            except ValidationError as exc:
                # Recorded as failed rather than dropped: the payload stays in
                # `events_raw` with the reason, so a schema fix can replay it
                # instead of leaving a permanent hole in the data.
                await raw_events.mark_failed(event_id, error=exc.json())
                await self._stream.dead_letter(item.payload, reason="schema_validation_failed")
                outcome.rejected += 1
                log.warning("event_rejected", event_id=str(event_id), errors=exc.error_count())
            else:
                inserted = await logs.upsert(event)
                await raw_events.mark_processed(event_id)

                if inserted:
                    outcome.inserted += 1
                else:
                    # Not an error: a redelivery correctly absorbed.
                    outcome.duplicates += 1

        # Outside the transaction, and only after it committed. Acking first
        # would discard the event while the write might still fail.
        await self._stream.ack(item.delivery_id)


def _extract_id(payload: dict[str, Any]) -> uuid.UUID | None:
    raw = payload.get("id")
    if not isinstance(raw, str):
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None
