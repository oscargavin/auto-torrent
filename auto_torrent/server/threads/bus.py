"""Bus for a turn that belongs to a conversation.

Splits the agent's output three ways, which the SMS-era code had to conflate:

  - prose written for a human ("Found it, narrated by Stephen Fry") becomes an
    assistant MESSAGE in the thread;
  - lifecycle narration ("Searching…", "Found 8 copies — checking each…")
    before anything commits becomes an ephemeral ACTIVITY event on the thread
    stream, which the client renders as a thinking row;
  - the same narration after a commit goes to the JOB stream, untouched, so the
    existing progress rail, staleness clock and stage icons keep working.

That last switch is what `bind_job` does. Before it, there is no job to attach
progress to — inventing one so the narration had somewhere to go was the design
this replaces.

Prose used to be published as a `progress` frame tagged `source: agent`, which
the client then had to throw away: a sentence written for SMS restates the book
by name, directly under a card already showing it. Given somewhere proper to
live, it stops being noise and becomes the reply.
"""

from __future__ import annotations

import logging

from ..jobs.bus import StreamEventBus
from ..jobs.events import EventLog
from .store import ThreadStore
from .types import EVENT_ACTIVITY, Message, MessageKind

logger = logging.getLogger("atb.threads.bus")


class ThreadSink(StreamEventBus):
    """A job bus that also speaks into a thread.

    Subclasses rather than wraps so the sync thread-safe wrappers and the
    `_last_narration` de-duplication cursor behave exactly as they do on the
    bare jobs path — only the routing changes.
    """

    def __init__(
        self,
        log: EventLog,
        *,
        thread_id: str,
        thread_log: EventLog,
        threads: ThreadStore,
    ) -> None:
        # No job yet: a turn that ends in a question never gets one.
        super().__init__("", log)
        self._thread_id = thread_id
        self._thread_log = thread_log
        self._threads = threads

    def bind_job(self, job_id: str) -> None:
        """Attach to a job, from which point progress routes to its stream."""
        self.job_id = job_id

    async def emit_async(self, type: str, data: dict | None = None) -> None:
        if self.job_id:
            await super().emit_async(type, data)
            return
        # Pre-commit: ephemeral, and deliberately not a message. The search
        # phase emits a line every few seconds; persisting each one would turn
        # a 90-second search into thirty lines of transcript nobody wants to
        # scroll past tomorrow.
        await self._thread_log.publish(self._thread_id, EVENT_ACTIVITY, data or {})
        self.messaged = True

    async def system_progress_async(self, text: str) -> None:
        await self.emit_async("progress", {"text": text})

    async def send_async(self, _phone: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        try:
            await self._threads.append(
                self._thread_id,
                Message.new(self._thread_id, MessageKind.assistant, text=text),
            )
        except Exception:  # noqa: BLE001
            # The agent's reply failing to persist must not fail the download it
            # is announcing. Losing a sentence is recoverable; losing the book
            # is not.
            logger.exception("thread %s: assistant message failed", self._thread_id)
        self.messaged = True
