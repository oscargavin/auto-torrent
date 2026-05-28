"""FastAPI router for /chat/jobs*. Built via factory so tests can inject fakes."""

from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sse_starlette.sse import EventSourceResponse

from ..app import _require_bearer  # re-use the existing bearer check
from ..worker import _kill_download_and_clean
from ...cli import _read_state
from .events import EventLog
from .store import JobStore
from .types import TERMINAL_STATUSES, CreateJobRequest, Job, JobStatus

logger = logging.getLogger("atb.jobs.api")


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

        # Derive terminal event types from the canonical status set. "error" and
        # "completed" are event-type aliases for "failed"/"succeeded" respectively.
        _TERMINAL_EVENT_TYPES = (
            frozenset(s.value for s in TERMINAL_STATUSES) | {"error", "completed"}
        )

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
                    if event["type"] in _TERMINAL_EVENT_TYPES:
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
        """Mark a job as cancelled, kill any running download, clean partial files.

        Three things happen in order:
        1. Flip state to `cancelled` (atomic, terminal-guarded — second DELETE is
           a no-op against the same record).
        2. If the agent had already committed and registered a `download_id`,
           look up the state file, SIGTERM the subprocess group, remove the
           state file, and `rmtree` the landing directory so a partial .m4b
           doesn't end up scanned into the ABS library.
        3. Publish the `cancelled` SSE event so any open subscriber tears down.
           The terminal-event filter on the stream means a later "error" event
           from the worker (raised when the killed subprocess crashes its poll)
           is published to Redis but no client receives it — the stream already
           closed on this `cancelled`.
        """
        pre = await store.get(job_id)
        if pre is None:
            raise HTTPException(status_code=404, detail="job not found")
        updated = await store.update_status(job_id, JobStatus.cancelled)
        if updated is None:
            # Race: job expired between the initial get and update_status.
            raise HTTPException(status_code=404, detail="job not found")
        # Only act on a fresh transition — update_status returns the unchanged
        # job for already-terminal jobs (Fix 1), so a re-DELETE is a no-op.
        if updated.status == JobStatus.cancelled and pre.status != JobStatus.cancelled:
            # Re-read to capture a download_id the worker may have written
            # between `pre` (above) and now — the worker's set_download_id
            # fires at the agent-commit boundary and can race a fast cancel.
            # The worker's own post-register status re-check covers the
            # symmetric race (cancel happens BEFORE register), so between
            # the two we don't leak an orphan.
            latest = await store.get(job_id)
            download_id = latest.download_id if latest else pre.download_id
            if download_id:
                await _kill_subprocess_and_clean(download_id)
            await log.publish(job_id, "cancelled", {})
        return {"ok": True, "status": updated.status.value}

    return router


async def _kill_subprocess_and_clean(download_id: str) -> None:
    """Look up the download's state file and tear everything down — kill the
    aria2 subprocess (with SIGKILL escalation), remove the partial landing
    directory, remove the state file. The heavy lifting lives in
    server/worker.py:_kill_download_and_clean; this helper just resolves the
    state by download_id (the only thing the cancel handler has on hand)."""
    try:
        state = _read_state(download_id)
    except Exception:  # noqa: BLE001
        logger.exception("cancel: read_state failed for %s", download_id)
        return
    if not state:
        return
    await _kill_download_and_clean(state)
