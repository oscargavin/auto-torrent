"""Event bus that publishes into an EventLog. ThreadSink builds on it.

The agent and the download poll call `bus.emit(...)` and `bus.send(...)` from
both coroutines and worker threads (`asyncio.to_thread`). The sync wrappers
schedule onto the running loop via `call_soon_threadsafe`, so a thread can
publish without its own event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .events import EventLog

logger = logging.getLogger("atb.jobs.bus")

# Strong references so GC cannot collect a task before it completes.
_PENDING_PUBLISHES: set[asyncio.Task] = set()


def _schedule(coro: Any) -> asyncio.Task:
    """Schedule a fire-and-forget publish from a thread callback.

    Keeps a strong ref so the task can't be GC'd, and logs any exception
    instead of letting it disappear into an unhandled-exception warning.
    """
    task = asyncio.create_task(coro)
    _PENDING_PUBLISHES.add(task)

    def _done(t: asyncio.Task) -> None:
        _PENDING_PUBLISHES.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.error("bus publish failed: %r", exc)

    task.add_done_callback(_done)
    return task


class StreamEventBus:
    def __init__(self, job_id: str, log: EventLog) -> None:
        self.job_id = job_id
        self._log = log
        # The bus is always constructed inside an async context (arq worker or SSE
        # handler), so get_running_loop() is guaranteed to succeed. Raising
        # RuntimeError here is the right failure mode — it means a construction
        # site moved outside async and needs fixing.
        self._loop = asyncio.get_running_loop()

    # --- async core ---

    async def emit_async(self, type: str, data: dict | None = None) -> None:
        await self._log.publish(self.job_id, type, data)

    async def send_async(self, text: str) -> None:
        # The text goes out as a "progress" event.
        #
        # `source: agent` marks this as free-form prose rather than a lifecycle
        # frame. It re-states the book by name and runs to any length, which
        # is why the card truncated it mid-word under the same title. Card
        # clients keep these out of the status line; the event stays in the log.
        await self.emit_async("progress", {"text": text, "source": "agent"})

    # --- sync wrappers (callable from threads) ---
    # Fire-and-forget is intentional: the event loop is owned by the running arq
    # job, so tasks created here always complete before the job function returns.
    # _schedule() keeps a strong ref and logs any Redis publish failure.

    def emit(self, type: str, data: dict | None = None) -> None:
        self._loop.call_soon_threadsafe(
            lambda: _schedule(self.emit_async(type, data))
        )

    def send(self, text: str) -> None:
        self._loop.call_soon_threadsafe(
            lambda: _schedule(self.send_async(text))
        )

    def close(self) -> None:
        # No-op; the stream is shared across many subscribers and stays alive
        # for the configured stream TTL. Worker-side completion is signalled by
        # publishing a `completed`/`failed` event.
        pass
