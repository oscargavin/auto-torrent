"""arq worker entrypoint. One function = one job; ctx carries shared resources."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from arq.connections import RedisSettings

from ..agent import run_agent
from ..app import _emit_download_and_poll  # re-uses the existing pump
from ..llm import clear_conversation, get_pending_options
from ..settings import Settings
from .bus import StreamEventBus
from .events import EventLog
from .store import JobStore
from .types import TERMINAL_STATUSES, JobStatus

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
        await bus.emit_async("progress", {"text": f"Searching for “{job.query}”…"})

        pending = get_pending_options(job.id)
        outcome = await run_agent(
            job.query, job.id, settings, bus, pending_options=pending
        )

        if outcome.kind == "committed":
            clear_conversation(job.id)
            await _emit_download_and_poll(
                bus,
                download=outcome.download or {},
                fallbacks=outcome.fallbacks,
                display=outcome.display,
                title=outcome.title,
                author=outcome.author,
                session=job.id,
            )
            await store.update_status(
                job.id,
                JobStatus.succeeded,
                picked_title=outcome.title,
                picked_author=outcome.author,
            )
            await bus.emit_async("completed", {"title": outcome.title, "author": outcome.author})
            await log.expire(job.id, settings.job_state_ttl_s)
        else:
            # asked / no_results / error — agent already published progress events.
            msg = outcome.message or f"agent ended: {outcome.kind}"
            await store.update_status(job.id, JobStatus.failed, error=msg)
            await log.expire(job.id, settings.job_state_ttl_s)
            if outcome.kind not in ("asked", "no_results") and not bus.messaged:
                await bus.emit_async("error", {"message": msg})

    except asyncio.CancelledError:
        logger.info("run_chat_job: cancelled (likely SIGTERM) for %s", job_id)
        await store.update_status(job.id, JobStatus.failed, error="worker cancelled")
        try:
            await bus.emit_async("error", {"message": "worker cancelled"})
        except Exception:  # noqa: BLE001
            pass  # bus publish may also fail mid-shutdown — best effort
        await log.expire(job.id, settings.job_state_ttl_s)
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("run_chat_job crashed for %s", job_id)
        await store.update_status(job.id, JobStatus.failed, error=f"{type(e).__name__}: {e}")
        await bus.emit_async("error", {"message": f"{type(e).__name__}: {e}"})
        await log.expire(job.id, settings.job_state_ttl_s)


class WorkerSettings:
    """arq config — `arq auto_torrent.server.jobs.worker.WorkerSettings`."""

    functions = [run_chat_job]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 4  # modest concurrency; downloads are I/O-bound but ABS scans are heavy
    job_timeout = 60 * 60  # 1h hard cap — matches existing POLL_TIMEOUT_S
    keep_result = settings.job_state_ttl_s

    # arq looks up `on_startup` / `on_shutdown` on WorkerSettings — NOT
    # `startup`/`shutdown` (silently ignored otherwise → ctx missing keys).
    @staticmethod
    async def on_startup(ctx: dict[str, Any]) -> None:
        from redis.asyncio import Redis

        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        ctx["redis"] = redis
        ctx["store"] = JobStore(
            redis,
            state_ttl_s=settings.job_state_ttl_s,
            dedup_ttl_s=settings.job_dedup_ttl_s,
        )
        ctx["log"] = EventLog(redis)

    @staticmethod
    async def on_shutdown(ctx: dict[str, Any]) -> None:
        await ctx["redis"].aclose()
