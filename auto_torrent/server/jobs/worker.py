"""arq worker entrypoint. One function = one job; ctx carries shared resources."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from arq.connections import RedisSettings

from ..agent import run_agent
from ..event_types import STAGE_SEARCHING
from ..settings import Settings
from ..threads.worker import run_thread_turn
from ..worker import JOB_BUDGET_S
from .bus import StreamEventBus
from .events import EventLog
from .finish import finish_agent_outcome
from .store import JobStore
from .types import TERMINAL_STATUSES, FailureClass, JobStatus

logger = logging.getLogger("atb.jobs.worker")
settings = Settings()


async def run_chat_job(ctx: dict[str, Any], job_id: str) -> None:
    store: JobStore = ctx["store"]
    log: EventLog = ctx["log"]

    job = await store.get(job_id)
    if job is None:
        logger.warning("run_chat_job: missing job %s", job_id)
        return

    if job.status in TERMINAL_STATUSES:
        logger.info("run_chat_job: job %s already %s, skipping", job_id, job.status.value)
        return

    await store.update_status(job.id, JobStatus.running)
    bus = StreamEventBus(job.id, log)

    try:
        await bus.emit_async("progress", {
            "stage": STAGE_SEARCHING,
            # No query echo: the card renders job.query as its headline, so
            # naming it here printed the same words twice, one line apart.
            "text": "Searching…",
        })

        # No pending-options lookup here: it read a process-local dict keyed by
        # job.id — a fresh uuid every job, in the API process rather than this
        # one — so it never once returned anything. allow_ask=False stops the
        # agent generating a question this channel cannot carry an answer to.
        outcome = await run_agent(job.query, job.id, settings, bus, allow_ask=False)
        await finish_agent_outcome(store=store, bus=bus, job=job, outcome=outcome)

    except asyncio.CancelledError:
        logger.info("run_chat_job: cancelled (likely SIGTERM) for %s", job_id)
        # update_status publishes the terminal event; no separate emit.
        await store.update_status(
            job.id,
            JobStatus.failed,
            error="This one was stopped before it finished.",
            failure_class=FailureClass.infra_error,
        )
        raise
    except Exception:  # noqa: BLE001
        # The traceback goes to the log, where it's useful. What reaches the
        # card is what a family member can act on — "RuntimeError: ..." is not.
        logger.exception("run_chat_job crashed for %s", job_id)
        await store.update_status(
            job.id,
            JobStatus.failed,
            error="Something went wrong on the server.",
            failure_class=FailureClass.infra_error,
        )



class WorkerSettings:
    """arq config — `arq auto_torrent.server.jobs.worker.WorkerSettings`."""

    functions = [run_chat_job, run_thread_turn]
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
