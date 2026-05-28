"""FastAPI router for /chat/jobs*. Built via factory so tests can inject fakes."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sse_starlette.sse import EventSourceResponse

from ..app import _require_bearer  # re-use the existing bearer check
from ..worker import _kill_download
from ...cli import _read_state
from ...config import STATE_DIR
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
            # Kill the subprocess + clean the landing dir BEFORE publishing the
            # event. If the kill raises, we still publish (the state flip is
            # what matters to the client) — but the partial file may linger.
            download_id = pre.download_id
            if download_id:
                _kill_subprocess_and_clean(download_id)
            await log.publish(job_id, "cancelled", {})
        return {"ok": True, "status": updated.status.value}

    return router


def _kill_subprocess_and_clean(download_id: str) -> None:
    """Best-effort: kill the aria2 subprocess, remove its state file, remove
    the partial landing directory. Logs and swallows every error — DELETE
    state-flip is the load-bearing part; cleanup is a courtesy."""
    try:
        state = _read_state(download_id)
    except Exception:  # noqa: BLE001
        logger.exception("cancel: read_state failed for %s", download_id)
        return
    if not state:
        return
    # Kill the running subprocess (process group, SIGTERM → SIGKILL fallback
    # already handled by _kill_download).
    try:
        _kill_download(state)
    except Exception:  # noqa: BLE001
        logger.exception("cancel: _kill_download failed for %s", download_id)
    # Remove the partial landing dir so a half-finished .m4b doesn't get
    # scanned into the ABS library.
    landing_path = state.get("path")
    if landing_path:
        try:
            shutil.rmtree(landing_path, ignore_errors=True)
        except Exception:  # noqa: BLE001
            logger.exception("cancel: rmtree %s failed", landing_path)
    # Remove the state file — _watch_until_done would otherwise keep polling
    # a now-dead PID, eventually timing out and logging noise.
    try:
        state_path = STATE_DIR / f"{download_id}.json"
        state_path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.exception("cancel: unlink state %s failed", download_id)
