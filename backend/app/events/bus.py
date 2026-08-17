"""Event transport, behind an interface.

The instrumentation wrapper depends on `EventBus`, never on Redis. That is what
makes the "swap Redis for Kafka" claim in the README an infrastructure change
rather than an application rewrite -- and, more immediately, what lets the
whole instrumentation path be tested without a broker.

`InMemoryEventBus` is a real implementation of both halves, not a stub: the
API can run against it with no Redis at all, and the tests use it to assert on
events without polling a stream.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.domain.events import InferenceEvent


@dataclass(frozen=True, slots=True)
class DeliveredEvent:
    """One event as handed to a consumer.

    `delivery_id` is the broker's handle for acknowledgement, deliberately
    distinct from the event's own id -- the same event redelivered arrives with
    a new delivery id but the same event id, which is exactly what makes the
    idempotent upsert work.
    """

    delivery_id: str
    payload: dict[str, Any]


class EventBus(ABC):
    """Producer side. Used by the request path, so it must never block it."""

    @abstractmethod
    async def publish(self, event: InferenceEvent) -> bool:
        """Publish an event.

        Returns whether it was accepted. Implementations must **not** raise on
        transport failure: a logging outage must degrade into lost telemetry,
        never into a failed chat response.
        """

    async def close(self) -> None:
        return None


class EventStream(ABC):
    """Consumer side. Used by the ingestion worker."""

    @abstractmethod
    async def ensure_group(self) -> None:
        """Create the consumer group if absent. Must be idempotent."""

    @abstractmethod
    async def poll(self, *, count: int, block_ms: int) -> list[DeliveredEvent]:
        """Fetch up to `count` events, waiting at most `block_ms`."""

    @abstractmethod
    async def ack(self, delivery_id: str) -> None:
        """Mark an event as processed so it is not redelivered."""

    @abstractmethod
    async def dead_letter(self, payload: dict[str, Any], *, reason: str) -> None:
        """Divert an unprocessable event.

        Dropping it would make a schema bug look like an absence of traffic;
        crashing on it would let one malformed event stop the pipeline.
        """

    async def close(self) -> None:
        return None


class InMemoryEventBus(EventBus, EventStream):
    """In-process transport.

    Lets the API run and the full instrumentation path be exercised with no
    broker -- useful for tests, and for anyone who wants to see the system work
    before standing up Redis.
    """

    def __init__(self) -> None:
        self._pending: list[DeliveredEvent] = []
        self._counter = 0
        self.published: list[InferenceEvent] = []
        self.dead_lettered: list[tuple[dict[str, Any], str]] = []

    async def publish(self, event: InferenceEvent) -> bool:
        self._counter += 1
        self.published.append(event)
        self._pending.append(
            DeliveredEvent(
                delivery_id=str(self._counter),
                payload=event.model_dump(mode="json"),
            )
        )
        return True

    async def ensure_group(self) -> None:
        return None

    async def poll(self, *, count: int, block_ms: int) -> list[DeliveredEvent]:
        batch, self._pending = self._pending[:count], self._pending[count:]
        return batch

    async def ack(self, delivery_id: str) -> None:
        return None

    async def dead_letter(self, payload: dict[str, Any], *, reason: str) -> None:
        self.dead_lettered.append((payload, reason))
