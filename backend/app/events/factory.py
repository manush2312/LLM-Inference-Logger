"""Chooses the event transport from configuration.

The one place that knows both backends exist. Everything downstream depends on
the `EventBus` interface, so switching transports is a config change here
rather than an edit spread across the instrumentation path.
"""

from __future__ import annotations

from app.core.config import EventBusBackend, Settings
from app.core.logging import get_logger
from app.events.bus import EventBus, InMemoryEventBus
from app.events.redis_bus import RedisEventBus, create_redis_client

log = get_logger(__name__)


def create_event_bus(settings: Settings) -> EventBus:
    if settings.event_bus_backend is EventBusBackend.MEMORY:
        # Events are still produced and still validated -- they just never
        # leave the process. Useful for running the API standalone; useless
        # for the worker, which says so on startup.
        log.warning("event_bus_in_memory", detail="events will not reach the ingestion worker")
        return InMemoryEventBus()

    return RedisEventBus(settings, create_redis_client(settings))
