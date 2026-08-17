"""Redis Streams transport.

Streams rather than pub/sub: pub/sub drops anything published while no
subscriber is connected, so every event produced during a worker restart would
vanish. A stream retains entries and consumer groups track per-consumer
progress, which is what makes "the worker was down for a minute" a delay rather
than a hole in the data.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import redis.asyncio as redis
from redis.exceptions import ResponseError

from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.events import InferenceEvent
from app.events.bus import DeliveredEvent, EventBus, EventStream

log = get_logger(__name__)

#: The whole event travels as JSON in one field. Redis stream values are flat
#: string maps, so spreading the event across fields would mean re-deriving
#: types (ints, timestamps, nulls, nested metadata) on the way out -- a second
#: place for the contract to be interpreted, and a second place to get it wrong.
_FIELD = "data"


class RedisEventBus(EventBus):
    """Producer. Lives in the request path, so it is bounded and never raises."""

    def __init__(self, settings: Settings, client: redis.Redis) -> None:
        self._client = client
        self._stream = settings.event_stream_name
        self._maxlen = settings.event_stream_maxlen
        self._timeout_s = settings.event_publish_timeout_s

    async def publish(self, event: InferenceEvent) -> bool:
        try:
            async with asyncio.timeout(self._timeout_s):
                await self._client.xadd(
                    self._stream,
                    {_FIELD: event.model_dump_json()},
                    # Bounded so a stalled worker degrades into dropped old
                    # events rather than an out-of-memory Redis taking the
                    # chat API down with it. `approximate` lets Redis trim on
                    # whole nodes, which is far cheaper than exact trimming.
                    maxlen=self._maxlen,
                    approximate=True,
                )
        except (TimeoutError, redis.RedisError) as exc:
            # Deliberately swallowed. This is the tradeoff stated in the
            # README: an observability outage costs telemetry, not user-facing
            # availability.
            log.warning(
                "event_publish_failed",
                event_id=str(event.id),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return False

        return True

    async def close(self) -> None:
        await self._client.aclose()


class RedisEventStream(EventStream):
    """Consumer. Lives in the worker.

    Reads through a consumer group, so several worker replicas can share one
    stream: Redis hands each entry to exactly one consumer, which is what makes
    the worker horizontally scalable with no coordination code of our own.
    """

    def __init__(self, settings: Settings, client: redis.Redis, *, consumer_name: str) -> None:
        self._client = client
        self._stream = settings.event_stream_name
        self._dlq = settings.event_stream_dlq
        self._group = settings.event_consumer_group
        self._consumer = consumer_name

    async def ensure_group(self) -> None:
        try:
            # mkstream so the worker can start before the API has ever
            # published; otherwise a cold start races the first request.
            await self._client.xgroup_create(self._stream, self._group, id="0", mkstream=True)
            log.info("consumer_group_created", stream=self._stream, group=self._group)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
            log.debug("consumer_group_exists", group=self._group)

    async def poll(self, *, count: int, block_ms: int) -> list[DeliveredEvent]:
        # The client is untyped at this boundary -- redis-py describes the
        # reply as `Any` because its shape depends on RESP version and on
        # `decode_responses`. Naming the shape once here keeps the cast out of
        # the worker and gives the rest of the codebase a typed surface.
        try:
            raw_response = await self._client.xreadgroup(
                groupname=self._group,
                consumername=self._consumer,
                streams={self._stream: ">"},
                count=count,
                block=block_ms,
            )
        except ResponseError as exc:
            if "NOGROUP" not in str(exc):
                raise
            # The stream or the group vanished underneath us -- an eviction, a
            # manual cleanup, a flushed dev database. Without this the worker
            # spins on the same error forever and silently stops ingesting,
            # while still looking alive to a liveness probe. Recreate and let
            # the next poll proceed.
            log.warning("consumer_group_missing", group=self._group, detail="recreating")
            await self.ensure_group()
            return []
        response = cast(list[tuple[str, list[tuple[str, dict[str, str]]]]], raw_response or [])

        delivered: list[DeliveredEvent] = []
        for _stream, entries in response:
            for delivery_id, fields in entries:
                payload = self._decode(delivery_id, fields)
                if payload is None:
                    # Unparseable at the transport level -- never even reached
                    # the schema. Ack it, or it is redelivered forever.
                    await self.dead_letter({"raw": str(fields)}, reason="malformed_json")
                    await self.ack(delivery_id)
                    continue
                delivered.append(DeliveredEvent(delivery_id=delivery_id, payload=payload))

        return delivered

    async def ack(self, delivery_id: str) -> None:
        await self._client.xack(self._stream, self._group, delivery_id)

    async def dead_letter(self, payload: dict[str, Any], *, reason: str) -> None:
        log.warning("event_dead_lettered", reason=reason)
        await self._client.xadd(
            self._dlq,
            {_FIELD: json.dumps(payload, default=str), "reason": reason},
            maxlen=10_000,
            approximate=True,
        )

    @staticmethod
    def _decode(delivery_id: str, fields: dict[str, str]) -> dict[str, Any] | None:
        raw = fields.get(_FIELD)
        if raw is None:
            return None
        try:
            decoded: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("event_decode_failed", delivery_id=delivery_id)
            return None
        return decoded

    async def close(self) -> None:
        await self._client.aclose()


def create_redis_client(settings: Settings) -> redis.Redis:
    """One place that builds a client, so timeouts are consistent everywhere."""
    return redis.from_url(
        settings.redis_url,
        # Values are JSON strings; decoding here keeps `bytes` out of the
        # rest of the codebase entirely.
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=2,
        health_check_interval=30,
    )
