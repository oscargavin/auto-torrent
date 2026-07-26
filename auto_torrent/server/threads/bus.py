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

import asyncio
import logging
import time

from ..jobs.bus import StreamEventBus, _schedule
from ..jobs.events import EventLog
from .store import ThreadStore
from .types import EVENT_ACTIVITY, Message, MessageKind, Step

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
        self._steps: list[Step] = []
        # Writes are scheduled fire-and-forget from tool callbacks, and each one
        # appends twice (its step group, then its message). Without a lock two
        # scheduled writes interleave and the transcript comes out as
        # steps/steps/text/text instead of steps/text/steps/text. Tasks acquire
        # in creation order, which is call order.
        self._write_lock = asyncio.Lock()
        # Own the tasks so the turn can wait for them; the module-level set in
        # jobs.bus is a GC guard, not a handle.
        self._writes: set[asyncio.Task] = set()

    def bind_job(self, job_id: str) -> None:
        """Attach to a job, from which point progress routes to its stream."""
        self.job_id = job_id

    async def emit_async(self, type: str, data: dict | None = None) -> None:
        if self.job_id:
            await super().emit_async(type, data)
            return
        # Pre-commit narration. Streamed live so the client can show the step
        # running, AND accumulated so the finished run can be persisted as one
        # collapsed group — the old version only streamed, so each line
        # replaced the last and the whole chain was gone by the time the agent
        # spoke.
        payload = data or {}
        text = str(payload.get("text") or "").strip()
        if text and (not self._steps or self._steps[-1].text != text):
            self._steps.append(
                Step(text=text, stage=str(payload.get("stage") or ""), at=time.time())
            )
        await self._thread_log.publish(self._thread_id, EVENT_ACTIVITY, payload)
        self.messaged = True

    def take_steps(self) -> list[Step]:
        """Detach the accumulated group. Synchronous on purpose — see `send`."""
        steps, self._steps = self._steps, []
        return steps

    async def flush_steps(self) -> None:
        """Close the current group and persist it.

        Called immediately before anything else is appended, so the steps land
        above the thing they led to. Assistant text between two runs of tool
        calls is what separates one group from the next — the same rule the
        chain-of-thought pattern uses — and that falls out of flushing here
        rather than needing to be detected.
        """
        await self._append_steps(self.take_steps())

    async def _append_steps(self, steps: list[Step]) -> None:
        if not steps:
            return
        try:
            await self._threads.append(
                self._thread_id,
                Message.new(self._thread_id, MessageKind.steps, steps=steps),
            )
        except Exception:  # noqa: BLE001
            logger.exception("thread %s: steps flush failed", self._thread_id)

    async def system_progress_async(self, text: str) -> None:
        await self.emit_async("progress", {"text": text})

    def send(self, _phone: str, text: str) -> None:
        """Say something, taking the current step group with it.

        The base class schedules the write and returns, which is what makes
        this override necessary: the steps belonging to a message are the ones
        accumulated before the agent decided to speak, not whatever has piled
        up by the time the scheduled write actually runs. Detaching them here,
        synchronously at call time, is what keeps a group from swallowing the
        tool calls that came *after* the sentence it belongs to.
        """
        if not (text or "").strip():
            return
        steps = self.take_steps()
        self.messaged = True
        self._loop.call_soon_threadsafe(
            lambda: self._track(_schedule(self._write_reply(text.strip(), steps)))
        )

    def _track(self, task: asyncio.Task) -> None:
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)

    async def drain(self) -> None:
        """Wait for every scheduled write to land.

        The turn ends by flushing leftover steps and setting the thread idle,
        and both of those must happen after the agent's own messages — not
        racing them into the middle of the transcript.
        """
        while self._writes:
            await asyncio.gather(*tuple(self._writes), return_exceptions=True)

    async def send_async(self, _phone: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.messaged = True
        await self._write_reply(text, self.take_steps())

    async def _write_reply(self, text: str, steps: list[Step]) -> None:
        # One coroutine, two awaits, under one lock — so the group and the
        # sentence it produced always land adjacent and the right way round.
        async with self._write_lock:
            await self._write_reply_locked(text, steps)

    async def _write_reply_locked(self, text: str, steps: list[Step]) -> None:
        await self._append_steps(steps)
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
