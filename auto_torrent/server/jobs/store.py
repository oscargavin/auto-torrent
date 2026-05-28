"""Redis-backed job store.

Layout:
  - HSET job:{id}                — Job fields (str→str)
  - SET  job:by_hash:{sha}       — job_id, TTL dedup_ttl_s; deleted on terminal status.
  - ZADD job:by_profile:{pid}    — score=created_at, member=job_id (for list endpoint)

Why a hash + secondary index, not Redis JSON: hashes are universally available,
and a profile's job list is naturally a sorted set keyed by recency.
"""

from __future__ import annotations

import time
from typing import Final

from redis.asyncio import Redis

from .types import (
    TERMINAL_STATUSES,
    CreateJobRequest,
    Job,
    JobStatus,
    dedup_hash,
)


def _job_key(job_id: str) -> str:
    return f"job:{job_id}"


def _hash_key(sha: str) -> str:
    return f"job:by_hash:{sha}"


def _profile_key(profile_id: str) -> str:
    return f"job:by_profile:{profile_id}"


class JobStore:
    def __init__(self, redis: Redis, *, state_ttl_s: int, dedup_ttl_s: int) -> None:
        self._r: Final[Redis] = redis
        self._state_ttl = state_ttl_s
        self._dedup_ttl = dedup_ttl_s

    async def create(self, req: CreateJobRequest) -> tuple[Job, bool]:
        """Idempotent create. Returns (job, created). If a non-terminal job
        already exists for this (profile, query), returns it with created=False."""
        sha = dedup_hash(req.profile_id, req.query)
        hash_key = _hash_key(sha)

        existing_id = await self._r.get(hash_key)
        if existing_id:
            existing = await self.get(existing_id)
            if existing and existing.status not in TERMINAL_STATUSES:
                return existing, False
            # Stale dedup key → drop and re-create.
            await self._r.delete(hash_key)

        job = Job.new(req.profile_id, req.query)
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.hset(_job_key(job.id), mapping=job.to_redis_hash())
            pipe.expire(_job_key(job.id), self._state_ttl)
            pipe.set(hash_key, job.id, ex=self._dedup_ttl)
            pipe.zadd(_profile_key(job.profile_id), {job.id: job.created_at})
            await pipe.execute()
        return job, True

    async def get(self, job_id: str) -> Job | None:
        data = await self._r.hgetall(_job_key(job_id))
        if not data:
            return None
        return Job.from_redis_hash(data)

    async def update_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        picked_title: str | None = None,
        picked_author: str | None = None,
        error: str | None = None,
    ) -> Job | None:
        job = await self.get(job_id)
        if job is None:
            return None
        job.status = status
        job.updated_at = time.time()
        if picked_title is not None:
            job.picked_title = picked_title
        if picked_author is not None:
            job.picked_author = picked_author
        if error is not None:
            job.error = error
        await self._r.hset(_job_key(job.id), mapping=job.to_redis_hash())
        if status in TERMINAL_STATUSES:
            # Release the dedup key so a re-request can start fresh.
            await self._r.delete(_hash_key(dedup_hash(job.profile_id, job.query)))
        return job
