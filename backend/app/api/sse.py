"""Server-Sent Events transport.

SSE rather than WebSockets: the stream is one-directional, it is ordinary
HTTP so it passes through proxies and load balancers unchanged, browsers
reconnect on their own, and Starlette streams it natively. A WebSocket would
buy bidirectionality this feature does not use, in exchange for a second
protocol to operate.

Two properties this module is responsible for:

* **Metadata and content are separate event types.** `ttft` is its own event
  rather than a field on the first content chunk, so a client rendering text
  never has to unpack timing data, and timing can be added to or changed
  without altering the shape of content events.
* **A disconnected client cancels the work behind the stream.** Without that,
  closing a tab leaves the model call running and billing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any, Final

from fastapi import Request

from app.core.logging import get_logger

log = get_logger(__name__)

#: How often to ask whether the client is still there. Short enough that an
#: abandoned model call is abandoned promptly; long enough not to spin.
DISCONNECT_POLL_SECONDS: Final = 0.15

#: Queue depth between the producer and the response. Bounded so a slow client
#: applies back-pressure rather than letting the producer buffer without limit.
_QUEUE_SIZE: Final = 64

_DONE = object()


def format_sse(event: str, data: dict[str, Any]) -> str:
    """Encode one event in the SSE wire format.

    The trailing blank line is the frame delimiter -- omit it and the client
    buffers forever waiting for an event that never appears terminated.
    """
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _watch_for_disconnect(request: Request, producer: asyncio.Task[None]) -> None:
    """Cancel the producer when the client goes away -- exactly once.

    The `return` after `cancel()` is load-bearing, not tidiness. `Task.cancel()`
    delivers `CancelledError` at the next checkpoint; it does not re-arm. But a
    watcher that keeps polling would call `cancel()` again on the next tick,
    and that second delivery lands while the instrumentation wrapper's `finally`
    is mid-publish -- destroying the very cancellation event it was recording.
    Measured: an unguarded watcher publishes zero events instead of one.

    `app.instrumentation.wrapper` shields its publish against exactly this, so
    the two layers are independent. This one keeps the pathological case from
    arising; that one keeps it from mattering if it ever does.
    """
    try:
        while not producer.done():
            if await request.is_disconnected():
                log.info("client_disconnected", detail="cancelling in-flight generation")
                producer.cancel()
                return
            await asyncio.sleep(DISCONNECT_POLL_SECONDS)
    except asyncio.CancelledError:
        # The stream finished normally and is tearing the watcher down.
        return


async def stream_with_disconnect_cancellation(
    request: Request, source: AsyncIterator[str]
) -> AsyncIterator[str]:
    """Forward `source` to the client, cancelling it on disconnect.

    The producer runs as its own task so the watcher has something cancellable
    to hold. Draining through a bounded queue keeps the response handler free
    to notice the producer ending, however it ends.
    """
    queue: asyncio.Queue[str | object] = asyncio.Queue(maxsize=_QUEUE_SIZE)

    async def produce() -> None:
        try:
            async for item in source:
                await queue.put(item)
        finally:
            # Always signals completion, including on cancellation, so the
            # consumer below can never be left waiting on a dead producer.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(_DONE)

    producer = asyncio.create_task(produce())
    watcher = asyncio.create_task(_watch_for_disconnect(request, producer))

    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                break
            yield item  # type: ignore[misc]
    finally:
        watcher.cancel()
        producer.cancel()
        # Awaited so the producer's own cleanup -- the wrapper's `finally`, and
        # therefore the cancellation event -- actually runs before the response
        # is torn down.
        for task in (producer, watcher):
            with contextlib.suppress(asyncio.CancelledError):
                await task
