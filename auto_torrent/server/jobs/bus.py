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


class StreamEventBus:
    def __init__(self, job_id: str, log: EventLog) -> None:
        self.job_id = job_id
        self._log = log
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g. sync test context); fall back.
            self._loop = asyncio.get_event_loop()
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

    def emit(self, type: str, data: dict | None = None) -> None:
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self.emit_async(type, data))
        )

    def send(self, _phone: str, text: str) -> None:
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self.send_async(_phone, text))
        )

    def system_progress(self, text: str) -> None:
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self.system_progress_async(text))
        )

    def close(self) -> None:
        # No-op; the stream is shared across many subscribers and stays alive
        # for the configured stream TTL. Worker-side completion is signalled by
        # publishing a `completed`/`failed` event.
        pass
