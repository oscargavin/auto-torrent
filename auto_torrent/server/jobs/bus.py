"""ChatEventBus-shaped adapter that publishes into an EventLog.

The existing `agent.py` + `worker.py` code calls `bus.emit(...)`, `bus.send(...)`,
`bus.system_progress(...)` from both async coroutines and sync threads
(`asyncio.to_thread`). We expose sync wrappers that schedule onto the running
loop via `call_soon_threadsafe` so non-async callers don't need rewrites.
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
        self.messaged = False

    # --- async core ---

    async def emit_async(self, type: str, data: dict | None = None) -> None:
        await self._log.publish(self.job_id, type, data)
        self.messaged = True

    async def send_async(self, _phone: str, text: str) -> None:
        # The agent calls bus.send(phone, text) — phone is the SMS API surface;
        # for chat we surface the text as a "progress" event.
        await self.emit_async("progress", {"text": text})

    async def system_progress_async(self, text: str) -> None:
        # Does NOT set messaged — mirrors ChatEventBus.system_progress behaviour.
        await self._log.publish(self.job_id, "progress", {"text": text})

    # --- sync wrappers (callable from threads) ---
    # Fire-and-forget is intentional: the event loop is owned by the running arq
    # job, so tasks created here always complete before the job function returns.
    # _schedule() keeps a strong ref and logs any Redis publish failure.

    def emit(self, type: str, data: dict | None = None) -> None:
        self._loop.call_soon_threadsafe(
            lambda: _schedule(self.emit_async(type, data))
        )

    def send(self, _phone: str, text: str) -> None:
        self._loop.call_soon_threadsafe(
            lambda: _schedule(self.send_async(_phone, text))
        )

    def system_progress(self, text: str) -> None:
        self._loop.call_soon_threadsafe(
            lambda: _schedule(self.system_progress_async(text))
        )

    def close(self) -> None:
        # No-op; the stream is shared across many subscribers and stays alive
        # for the configured stream TTL. Worker-side completion is signalled by
        # publishing a `completed`/`failed` event.
        pass
