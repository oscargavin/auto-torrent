"""FastAPI router for /chat/jobs*. Built via factory so tests can inject fakes."""

from __future__ import annotations

import json
from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sse_starlette.sse import EventSourceResponse

from ..app import _require_bearer  # re-use the existing bearer check
from .events import EventLog
from .store import JobStore
from .types import TERMINAL_STATUSES, CreateJobRequest, Job, JobStatus


def build_router(
    *,
    store: JobStore,
    log: EventLog,
    enqueue: Callable[[str], Awaitable[None]],
) -> APIRouter:
    router = APIRouter()

    @router.post("/chat/jobs", response_model=Job)
    async def create_job(
        req: CreateJobRequest,
        response: Response,
        _: None = Depends(_require_bearer),
    ) -> Job:
        job, created = await store.create(req)
        if created:
            await enqueue(job.id)
            response.status_code = 201
        else:
            response.status_code = 200
        return job

    @router.get("/chat/jobs/{job_id}", response_model=Job)
    async def get_job(job_id: str, _: None = Depends(_require_bearer)) -> Job:
        job = await store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @router.get("/chat/jobs", response_model=list[Job])
    async def list_jobs(
        profile_id: str,
        limit: int = 20,
        _: None = Depends(_require_bearer),
    ) -> list[Job]:
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=400, detail="limit must be 1..100")
        return await store.list_for_profile(profile_id, limit=limit)

    @router.get("/chat/jobs/{job_id}/events")
    async def stream_events(
        job_id: str,
        request: Request,
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
        _: None = Depends(_require_bearer),
    ) -> EventSourceResponse:
        # Refuse if the job doesn't exist; otherwise stream until the client
        # disconnects (the download continues regardless — disconnects are not
        # cancellations).
        if (await store.get(job_id)) is None:
            raise HTTPException(status_code=404, detail="job not found")

        _TERMINAL_EVENTS = frozenset({"completed", "error", "cancelled"})

        async def gen():
            # Poll in short windows so client disconnect is detected promptly
            # even while xread is blocking. Each window is at most 2s.
            POLL_S = 2.0
            subscriber = log.subscribe(
                job_id,
                since=last_event_id,
                idle_timeout_s=POLL_S,
            )
            try:
                async for event_id, event in subscriber:
                    if await request.is_disconnected():
                        return
                    if event["type"] == "keepalive":
                        # sse-starlette emits its own keepalive comments; we
                        # skip internal keepalives — the disconnect check above
                        # runs on every iteration so we still catch disconnects.
                        continue
                    yield {
                        "id": event_id,
                        "event": event["type"],
                        "data": json.dumps(event["data"]),
                    }
                    # Stop streaming once the job reaches a terminal state —
                    # no further events will be published after this point.
                    if event["type"] in _TERMINAL_EVENTS:
                        return
            finally:
                await subscriber.aclose()

        # X-Accel-Buffering header (Cloudflare/nginx) defeats proxy buffering of
        # text/event-stream; sse-starlette sets the rest.
        return EventSourceResponse(
            gen(),
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    @router.delete("/chat/jobs/{job_id}")
    async def cancel_job(job_id: str, _: None = Depends(_require_bearer)) -> dict:
        """Mark a job as cancelled and notify any open SSE subscribers.

        This is a state-only signal — the BG aria2 download process is NOT
        aborted here (aborting the arq worker mid-download is future work).
        _TERMINAL_EVENT_TYPES already includes "cancelled", so any open SSE
        stream will close on receiving this event.
        """
        pre = await store.get(job_id)
        if pre is None:
            raise HTTPException(status_code=404, detail="job not found")
        updated = await store.update_status(job_id, JobStatus.cancelled)
        if updated is None:
            # Race: job expired between the initial get and update_status.
            raise HTTPException(status_code=404, detail="job not found")
        # Only publish a fresh cancelled event when this call actually transitioned
        # the state — update_status returns the unchanged job for already-terminal
        # jobs (Fix 1), so compare pre-state.
        if updated.status == JobStatus.cancelled and pre.status != JobStatus.cancelled:
            await log.publish(job_id, "cancelled", {})
        return {"ok": True, "status": updated.status.value}

    return router
