"""Redis-backed thread store.

Layout:
  - HSET  thread:{id}                  — Thread fields (str→str)
  - RPUSH thread:{id}:messages         — one JSON Message per entry, in order
  - ZADD  thread:by_profile:{pid}      — score=updated_at, member=thread_id
  - SET   thread:{id}:pending          — magnets for the open choice (never sent
                                          to the client), TTL-bounded

A list rather than a stream for messages: the transcript is read whole on open
and appended to a handful of times per thread, and a list gives ordered reads
with no cursor bookkeeping. The *event* stream (EventLog) is separate and is
what carries live updates — the list is the durable record.
"""

from __future__ import annotations

import json
import time
from typing import Final

from redis.asyncio import Redis

from ..jobs.events import EventLog
from .types import (
    EVENT_MESSAGE,
    EVENT_STATUS,
    HISTORY_LIMIT,
    ChoiceOption,
    Message,
    MessageKind,
    Thread,
    ThreadStatus,
)

# How long an unanswered question stays answerable. Past this the magnets are
# very likely dead anyway, so re-running the search is more honest than acting
# on a stale pick.
PENDING_TTL_S = 30 * 60

# A thread stuck in `working` this long has lost its worker (OOM, host reboot,
# SIGKILL). Nothing else notices, so the read path un-sticks it — the same
# reasoning as JobStore._maybe_reap.
STUCK_AFTER_S = 20 * 60


def _thread_key(thread_id: str) -> str:
    return f"thread:{thread_id}"


def _messages_key(thread_id: str) -> str:
    return f"thread:{thread_id}:messages"


def _profile_key(profile_id: str) -> str:
    return f"thread:by_profile:{profile_id}"


def _pending_key(thread_id: str) -> str:
    return f"thread:{thread_id}:pending"


class ThreadStore:
    def __init__(self, redis: Redis, log: EventLog, *, state_ttl_s: int) -> None:
        self._r: Final[Redis] = redis
        self._log: Final[EventLog] = log
        self._ttl = state_ttl_s

    # ---- threads ----

    async def create(self, profile_id: str) -> Thread:
        thread = Thread.new(profile_id)
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.hset(_thread_key(thread.id), mapping=thread.to_redis_hash())
            pipe.expire(_thread_key(thread.id), self._ttl)
            pipe.zadd(_profile_key(profile_id), {thread.id: thread.updated_at})
            await pipe.execute()
        return thread

    async def _fetch(self, thread_id: str) -> Thread | None:
        data = await self._r.hgetall(_thread_key(thread_id))
        return Thread.from_redis_hash(data) if data else None

    async def get(self, thread_id: str) -> Thread | None:
        return await self._unstick(await self._fetch(thread_id))

    async def _unstick(self, thread: Thread | None) -> Thread | None:
        """Return a thread that can no longer make progress to `idle`.

        Two ways in, both ending with a conversation nothing will ever move:

        - `working` with a dead worker (OOM, SIGKILL, host reboot). Nothing else
          notices, so the read path has to.
        - `awaiting_choice` past the pending TTL. The magnets are gone, so every
          tap now 409s or 410s — the question is on screen and unanswerable.

        Deliberately silent in both cases: the user's message is still in the
        transcript and re-sending it is the natural recovery. Announcing "the
        server dropped this" would be accurate and useless.
        """
        if thread is None:
            return thread
        if thread.status is ThreadStatus.working:
            limit = STUCK_AFTER_S
        elif thread.status is ThreadStatus.awaiting_choice:
            limit = PENDING_TTL_S
        else:
            return thread
        if time.time() - thread.updated_at <= limit:
            return thread
        return await self.set_status(thread.id, ThreadStatus.idle) or thread

    async def set_status(self, thread_id: str, status: ThreadStatus) -> Thread | None:
        current = await self._fetch(thread_id)
        if current is None:
            return None
        updated_at = time.time()
        await self._r.hset(
            _thread_key(thread_id),
            mapping={"status": status.value, "updated_at": str(updated_at)},
        )
        await self._r.zadd(_profile_key(current.profile_id), {thread_id: updated_at})
        await self._r.expire(_thread_key(thread_id), self._ttl)
        await self._log.publish(thread_id, EVENT_STATUS, {"status": status.value})
        return current.model_copy(update={"status": status, "updated_at": updated_at})

    async def list_for_profile(self, profile_id: str, *, limit: int = 20) -> list[Thread]:
        ids = await self._r.zrevrange(_profile_key(profile_id), 0, limit - 1)
        out: list[Thread] = []
        for tid in ids:
            thread = await self.get(tid)
            if thread is not None:
                out.append(thread)
        return out

    # ---- messages ----

    async def append(self, thread_id: str, message: Message) -> Message:
        """Persist a message and publish it. One method, so a message can never
        reach the transcript without reaching live subscribers, or vice versa."""
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.rpush(_messages_key(thread_id), message.model_dump_json())
            pipe.expire(_messages_key(thread_id), self._ttl)
            await pipe.execute()
        # The thread's title is its first user line — set once, so a later
        # message can't rename a thread the user already recognises.
        if message.kind is MessageKind.user:
            current = await self._fetch(thread_id)
            if current is not None and not current.title:
                await self._r.hset(
                    _thread_key(thread_id), "title", message.text[:80]
                )
        await self._log.publish(
            thread_id, EVENT_MESSAGE, {"message": message.model_dump(mode="json")}
        )
        return message

    async def messages(self, thread_id: str, *, limit: int = 200) -> list[Message]:
        raw = await self._r.lrange(_messages_key(thread_id), -limit, -1)
        out: list[Message] = []
        for entry in raw:
            try:
                out.append(Message.model_validate_json(entry))
            except Exception:  # noqa: BLE001
                # One unreadable row must not blank the whole transcript.
                continue
        return out

    async def history_for_agent(self, thread_id: str) -> list[Message]:
        """The tail of the transcript, for prompting. Job messages are dropped —
        a job's status is not something the agent should reason about, and the
        turn that follows a download is nearly always a fresh request."""
        msgs = await self.messages(thread_id, limit=HISTORY_LIMIT * 2)
        return [m for m in msgs if m.kind is not MessageKind.job][-HISTORY_LIMIT:]

    async def resolve_choice(
        self, thread_id: str, message_id: str, option_index: int
    ) -> Message | None:
        """Mark a choice answered. Returns the updated message, or None if the
        message is missing, not a choice, already answered, or the index is out
        of range — all of which the API turns into a 4xx rather than a silent
        no-op, because each one means the client and server disagree."""
        raw = await self._r.lrange(_messages_key(thread_id), 0, -1)
        for pos, entry in enumerate(raw):
            try:
                msg = Message.model_validate_json(entry)
            except Exception:  # noqa: BLE001
                continue
            if msg.id != message_id:
                continue
            if msg.kind is not MessageKind.choice or msg.chosen_index is not None:
                return None
            if not any(o.index == option_index for o in msg.options):
                return None
            updated = msg.model_copy(update={"chosen_index": option_index})
            await self._r.lset(_messages_key(thread_id), pos, updated.model_dump_json())
            await self._log.publish(
                thread_id, EVENT_MESSAGE, {"message": updated.model_dump(mode="json")}
            )
            return updated
        return None

    # ---- pending magnets (server-side only) ----

    async def set_pending(self, thread_id: str, options: list[dict]) -> None:
        """Stash the full agent-side option payloads (magnets included).

        Kept out of the Message so a transcript is safe to render, log, or hand
        to the model without carrying torrent links through any of them.
        """
        await self._r.set(
            _pending_key(thread_id), json.dumps(options), ex=PENDING_TTL_S
        )

    async def take_pending(self, thread_id: str) -> list[dict]:
        raw = await self._r.get(_pending_key(thread_id))
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    async def clear_pending(self, thread_id: str) -> None:
        await self._r.delete(_pending_key(thread_id))


def _known(value: object) -> str:
    """Placeholder strings from the scraper, normalised to absent."""
    text = str(value or "").strip()
    return "" if text.lower() in {"", "unknown", "n/a", "none"} else text


def option_from_payload(index: int, payload: dict) -> ChoiceOption:
    """Agent-side result dict → the client-safe option.

    Field names differ deliberately: the agent's payload mirrors the scraper
    (`format`), while the wire type avoids shadowing the Python builtin and the
    JS reserved-ish `format`. One conversion, here, so neither side has to know
    about the other's naming.
    """
    return ChoiceOption(
        index=index,
        title=payload.get("title") or "Unknown",
        author=payload.get("author") or "",
        narrator=payload.get("narrator") or "",
        # The scraper writes the literal string "unknown" when it can't parse a
        # format. Rendering that puts the word "unknown" in a metadata line
        # whose whole job is to help someone choose — an absent field is more
        # honest and reads better than a confident "unknown".
        book_format=_known(payload.get("format")),
        size=_known(payload.get("size")),
        cover_url=payload.get("cover_url") or "",
        abridged=bool(payload.get("abridged")),
        note=payload.get("note") or "",
    )
