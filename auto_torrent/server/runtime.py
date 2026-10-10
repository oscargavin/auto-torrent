"""The Redis-backed objects that the web app, the arq worker and the nightly
watch-list check all need. One place builds them, so the three cannot drift."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .jobs.events import EventLog
from .jobs.store import JobStore
from .settings import Settings
from .threads.store import ThreadStore

if TYPE_CHECKING:
    from arq.connections import ArqRedis
    from redis.asyncio import Redis

EnqueueTurn = Callable[..., Awaitable[None]]


@dataclass
class Stores:
    log: EventLog
    thread_log: EventLog
    jobs: JobStore
    threads: ThreadStore


def build_stores(redis: Redis, settings: Settings) -> Stores:
    log = EventLog(redis)
    # Separate key prefix, not a shared stream: a thread outlives the jobs
    # inside it, so sharing would expire the conversation on the first job's
    # terminal event.
    thread_log = EventLog(redis, prefix="thread")
    return Stores(
        log=log,
        thread_log=thread_log,
        jobs=JobStore(
            redis,
            log,
            state_ttl_s=settings.job_state_ttl_s,
            dedup_ttl_s=settings.job_dedup_ttl_s,
        ),
        threads=ThreadStore(redis, thread_log, state_ttl_s=settings.job_state_ttl_s),
    )


async def turn_queue(settings: Settings) -> tuple[ArqRedis, EnqueueTurn]:
    """An arq pool, and the function that queues one chat turn on it. The
    caller closes the pool."""
    from arq import create_pool
    from arq.connections import RedisSettings

    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))

    async def enqueue_turn(
        thread_id: str, text: str, pending: list[dict] | None, event: str | None = None
    ) -> None:
        await pool.enqueue_job("run_thread_turn", thread_id, text, pending, event)

    return pool, enqueue_turn
