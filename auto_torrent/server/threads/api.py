"""FastAPI router for /chat/threads*. Factory-built so tests can inject fakes."""

from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from ..app import _require_bearer
from ..jobs.events import EventLog
from .store import ThreadStore
from .types import (
    ChoiceKind,
    ChooseRequest,
    CreateThreadRequest,
    Message,
    MessageKind,
    PostMessageRequest,
    Thread,
    ThreadStatus,
    ThreadView,
)

logger = logging.getLogger("atb.threads.api")

# Matches the worker's phrasing so the transcript reads the same whether the
# message came from a tap or was reconstructed from the pending payload.
CHOICE_ECHO = "That one: {title}"


def build_router(
    *,
    threads: ThreadStore,
    log: EventLog,
    enqueue_turn: Callable[[str, str, list[dict] | None], Awaitable[None]],
) -> APIRouter:
    router = APIRouter()

    async def _require_thread(thread_id: str) -> Thread:
        thread = await threads.get(thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="thread not found")
        return thread

    @router.post("/chat/threads", response_model=Thread, status_code=201)
    async def create_thread(
        req: CreateThreadRequest, _: None = Depends(_require_bearer)
    ) -> Thread:
        return await threads.create(req.profile_id)

    @router.get("/chat/threads", response_model=list[Thread])
    async def list_threads(
        profile_id: str, limit: int = 20, _: None = Depends(_require_bearer)
    ) -> list[Thread]:
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=400, detail="limit must be 1..100")
        return await threads.list_for_profile(profile_id, limit=limit)

    @router.get("/chat/threads/{thread_id}", response_model=ThreadView)
    async def get_thread(
        thread_id: str, _: None = Depends(_require_bearer)
    ) -> ThreadView:
        thread = await _require_thread(thread_id)
        return ThreadView(thread=thread, messages=await threads.messages(thread_id))

    @router.post("/chat/threads/{thread_id}/messages", response_model=Message)
    async def post_message(
        thread_id: str,
        req: PostMessageRequest,
        _: None = Depends(_require_bearer),
    ) -> Message:
        thread = await _require_thread(thread_id)
        # A turn already running would be talked over: the agent has its history
        # from before this message and would answer the wrong question. 409 is
        # the honest answer; the client disables the composer on `working` so
        # this is a race guard, not a user-facing state.
        if thread.status is ThreadStatus.working:
            raise HTTPException(status_code=409, detail="still working on the last one")

        # A new message supersedes an unanswered question — the user typed
        # instead of tapping, which is itself an answer ("none of those").
        if thread.status is ThreadStatus.awaiting_choice:
            await threads.clear_pending(thread_id)

        message = await threads.append(
            thread_id, Message.new(thread_id, MessageKind.user, text=req.text)
        )
        await enqueue_turn(thread_id, req.text, None)
        return message

    @router.post("/chat/threads/{thread_id}/choose", response_model=Message)
    async def choose(
        thread_id: str,
        req: ChooseRequest,
        _: None = Depends(_require_bearer),
    ) -> Message:
        await _require_thread(thread_id)
        resolved = await threads.resolve_choice(
            thread_id, req.message_id, req.option_index
        )
        if resolved is None:
            # Missing, not a choice, already answered, or an index that isn't
            # on the message. All four mean the client is acting on a stale
            # view, and all four are better as an error than as a silent
            # download of something the user didn't pick.
            raise HTTPException(status_code=409, detail="that choice is no longer open")

        pending = await threads.take_pending(thread_id)
        chosen = pending[req.option_index] if req.option_index < len(pending) else None
        if chosen is None:
            raise HTTPException(status_code=410, detail="those options have expired")

        option = next(
            (o for o in resolved.options if o.index == req.option_index), None
        )
        title = option.title if option else (chosen.get("title") or "that one")
        # Echoed as a user message so the transcript reads as a conversation
        # rather than as a card that silently changed state.
        await threads.append(
            thread_id,
            Message.new(thread_id, MessageKind.user, text=CHOICE_ECHO.format(title=title)),
        )

        # The agent told us which question it asked, so this dispatches on that
        # rather than reconstructing it from whether the option happens to carry
        # a magnet. Older messages predate the field and fall back to the
        # inference they were written under.
        kind = resolved.choice_kind or (
            ChoiceKind.edition if chosen.get("magnet") else ChoiceKind.book
        )
        # An edition's magnet is known and the only thing left is to start it.
        # Only the chosen option goes back — handing the agent the whole list
        # again would let it re-decide what the user already decided.
        if kind is ChoiceKind.edition and chosen.get("magnet"):
            await enqueue_turn(thread_id, f"Download {title}", [chosen])
        else:
            # A book the agent named without searching ("something like Name of
            # the Wind" means naming books, not torrents). Nothing to commit —
            # the next turn searches for the one they picked.
            author = (chosen.get("author") or "").strip()
            request = f"{title} by {author}" if author else title
            await enqueue_turn(thread_id, request, None)
        return resolved

    @router.get("/chat/threads/{thread_id}/events")
    async def stream_events(
        thread_id: str,
        request: Request,
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
        _: None = Depends(_require_bearer),
    ) -> EventSourceResponse:
        await _require_thread(thread_id)

        async def gen():
            # A thread stream never terminates on its own: unlike a job, a
            # conversation has no terminal state. It ends when the client
            # disconnects, which the 2s poll window detects promptly.
            subscriber = log.subscribe(thread_id, since=last_event_id, idle_timeout_s=2.0)
            try:
                async for event_id, event in subscriber:
                    if await request.is_disconnected():
                        return
                    if event["type"] == "keepalive":
                        continue
                    yield {
                        "id": event_id,
                        "event": event["type"],
                        "data": json.dumps(event["data"]),
                    }
            finally:
                await subscriber.aclose()

        return EventSourceResponse(
            gen(),
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    return router
