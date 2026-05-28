"""FastAPI router for /chat/jobs*. Built via factory so tests can inject fakes."""

from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Response

from ..app import _require_bearer  # re-use the existing bearer check
from .events import EventLog
from .store import JobStore
from .types import CreateJobRequest, Job


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

    return router
