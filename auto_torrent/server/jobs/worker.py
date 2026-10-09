"""arq worker entrypoint. One function = one job; ctx carries shared resources."""

from __future__ import annotations

from typing import Any

from arq.connections import RedisSettings

from ..settings import Settings
from ..threads.worker import run_thread_turn
from ..worker import JOB_BUDGET_S
from .events import EventLog
from .store import JobStore

settings = Settings()


class WorkerSettings:
    """arq config — `arq auto_torrent.server.jobs.worker.WorkerSettings`."""

    functions = [run_thread_turn]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 4  # modest concurrency; downloads are I/O-bound but ABS scans are heavy
    # Deliberately ABOVE the poll layer's own JOB_BUDGET_S so arq is a backstop
    # for a wedged worker, not the thing that normally ends a long job. When the
    # two were equal, any job that swapped to a fallback got killed here
    # mid-attempt and the user was told "worker cancelled".
    job_timeout = JOB_BUDGET_S + 10 * 60
    keep_result = settings.job_state_ttl_s

    # arq looks up `on_startup` / `on_shutdown` on WorkerSettings — NOT
    # `startup`/`shutdown` (silently ignored otherwise → ctx missing keys).
    @staticmethod
    async def on_startup(ctx: dict[str, Any]) -> None:
        from redis.asyncio import Redis

        from ..threads.store import ThreadStore

        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        log = EventLog(redis)
        thread_log = EventLog(redis, prefix="thread")
        ctx["redis"] = redis
        ctx["log"] = log
        ctx["thread_log"] = thread_log
        ctx["store"] = JobStore(
            redis,
            log,
            state_ttl_s=settings.job_state_ttl_s,
            dedup_ttl_s=settings.job_dedup_ttl_s,
        )
        ctx["threads"] = ThreadStore(
            redis, thread_log, state_ttl_s=settings.job_state_ttl_s
        )

    @staticmethod
    async def on_shutdown(ctx: dict[str, Any]) -> None:
        await ctx["redis"].aclose()
