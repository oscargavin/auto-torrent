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

from .events import EventLog
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
    def __init__(
        self,
        redis: Redis,
        log: EventLog,
        *,
        state_ttl_s: int,
        dedup_ttl_s: int,
    ) -> None:
        self._r: Final[Redis] = redis
        self._log: Final[EventLog] = log
        self._state_ttl = state_ttl_s
        self._dedup_ttl = dedup_ttl_s

    async def create(self, req: CreateJobRequest) -> tuple[Job, bool]:
        """Idempotent create. Returns (job, created)."""
        sha = dedup_hash(req.profile_id, req.query)
        hash_key = _hash_key(sha)

        for _ in range(3):
            job = Job.new(req.profile_id, req.query)
            # Atomic claim: SET NX wins exactly once per (profile, query) within the
            # dedup TTL. If it loses, another caller is already in flight — fetch and
            # return that job (or treat as stale if its key vanished mid-race).
            claimed = await self._r.set(hash_key, job.id, ex=self._dedup_ttl, nx=True)
            if claimed:
                # We own the dedup key. Write the job state + index.
                async with self._r.pipeline(transaction=True) as pipe:
                    pipe.hset(_job_key(job.id), mapping=job.to_redis_hash())
                    pipe.expire(_job_key(job.id), self._state_ttl)
                    pipe.zadd(_profile_key(job.profile_id), {job.id: job.created_at})
                    await pipe.execute()
                return job, True

            existing_id = await self._r.get(hash_key)
            if existing_id:
                existing = await self.get(existing_id)
                if existing and existing.status not in TERMINAL_STATUSES:
                    return existing, False
                # Stale dedup key (job vanished or terminal) — drop and retry.
                await self._r.delete(hash_key)
                continue
            # Race: hash_key vanished between SET NX failing and GET. Retry.
            continue

        # TTL must have flapped 3 times — Redis is under heavy churn or there is a bug.
        raise RuntimeError("jobs/store: dedup race did not converge in 3 attempts")

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
        current = await self.get(job_id)
        if current is None:
            return None
        # Terminal status is final — refuse any further transitions (including
        # same-status re-writes). Returning the unchanged job lets callers (e.g.
        # DELETE) treat re-cancellation as a no-op rather than 404.
        if current.status in TERMINAL_STATUSES:
            return current

        updated_at = time.time()
        fields: dict[str, str] = {
            "status": status.value,
            "updated_at": str(updated_at),
        }
        if picked_title is not None:
            fields["picked_title"] = picked_title
        if picked_author is not None:
            fields["picked_author"] = picked_author
        if error is not None:
            fields["error"] = error
        await self._r.hset(_job_key(job_id), mapping=fields)

        if status in TERMINAL_STATUSES and current.status not in TERMINAL_STATUSES:
            # First terminal write → release the dedup key so a re-request can
            # start fresh, and expire the event stream alongside the job hash.
            await self._r.delete(_hash_key(dedup_hash(current.profile_id, current.query)))
            await self._log.expire(job_id, self._state_ttl)

        # Build the updated job locally — avoids a third Redis round-trip.
        updated = current.model_copy(
            update={
                "status": status,
                "updated_at": updated_at,
                **({"picked_title": picked_title} if picked_title is not None else {}),
                **({"picked_author": picked_author} if picked_author is not None else {}),
                **({"error": error} if error is not None else {}),
            }
        )
        return updated

    async def set_download_id(self, job_id: str, download_id: str) -> None:
        """Register the running download's state-file id against the job.

        This deliberately bypasses the terminal-state guard in update_status:
        the field is metadata (a pointer to the subprocess), not a transition.
        If the job was just cancelled between the agent committing and this
        write, we still want the pointer so cancel_job can find + kill the
        already-spawned subprocess (otherwise the cancel is half-done — state
        flipped but the aria2 fetch keeps running to completion).
        """
        await self._r.hset(_job_key(job_id), "download_id", download_id)

    async def list_for_profile(self, profile_id: str, *, limit: int = 20) -> list[Job]:
        # ZRANGEBYSCORE with REV — most recent first.
        ids = await self._r.zrevrange(_profile_key(profile_id), 0, limit - 1)
        if not ids:
            return []
        async with self._r.pipeline(transaction=False) as pipe:
            for jid in ids:
                pipe.hgetall(_job_key(jid))
            rows = await pipe.execute()
        return [Job.from_redis_hash(r) for r in rows if r]
