"""Worker entrypoint (`llm-worker`).

A separate process from the API, sharing the same `app` package. Sharing the
package is the point: the event contract, the ORM models and the repositories
are imported, not reimplemented, so producer and consumer physically cannot
drift out of sync.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket

from app.core.config import EventBusBackend, Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import Database
from app.events.bus import EventStream
from app.events.redis_bus import RedisEventStream, create_redis_client
from app.worker.consumer import IngestionWorker

log = get_logger(__name__)


def _consumer_name() -> str:
    """Identify this replica within the consumer group.

    Redis tracks pending entries per consumer name, so two replicas sharing one
    name would be able to acknowledge each other's in-flight work. Host plus
    pid is unique per replica in both Docker and Kubernetes.
    """
    return f"{socket.gethostname()}-{os.getpid()}"


def _build_stream(settings: Settings) -> EventStream:
    if settings.event_bus_backend is EventBusBackend.MEMORY:
        # An in-process bus has nothing for a separate process to consume;
        # failing loudly beats a worker that runs forever reading an empty
        # queue and looks healthy while ingesting nothing.
        raise RuntimeError(
            "EVENT_BUS_BACKEND=memory has no cross-process stream; "
            "set EVENT_BUS_BACKEND=redis to run the worker."
        )

    return RedisEventStream(settings, create_redis_client(settings), consumer_name=_consumer_name())


async def _run() -> None:
    settings = get_settings()
    configure_logging(settings)

    stream = _build_stream(settings)
    database = Database(settings)
    worker = IngestionWorker(stream=stream, database=database)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Graceful shutdown: finish the in-flight batch and ack it rather than
        # dying mid-write. Kubernetes sends SIGTERM before it removes a pod.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.request_stop)

    try:
        await worker.run_forever()
    finally:
        await stream.close()
        await database.dispose()


def run() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    run()
