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
            # The agent has already spawned the download subprocess; register
            # its state-file id against the job so a subsequent DELETE can find
            # the running PID + landing path and tear them down.
            download_id = (outcome.download or {}).get("id")
            if download_id:
                await store.set_download_id(job.id, download_id)
            # Re-check status: cancel may have fired during the agent's search
            # (which can take 10–30s). If so, skip the poll — _emit_download
            # would otherwise loop on the subprocess that cancel_job is about
            # to kill (or already killed), producing a confusing extra error
            # event after the user already saw cancelled.
            current = await store.get(job.id)
            if current and current.status == JobStatus.cancelled:
                logger.info(
                    "run_chat_job: %s cancelled during agent run; not entering poll",
                    job.id,
                )
                return
            await _emit_download_and_poll(
                bus,
                download=outcome.download or {},
                fallbacks=outcome.fallbacks,
                display=outcome.display,
                title=outcome.title,
                author=outcome.author,
                session=job.id,
            )
            # If cancel fired during the poll, _emit_download_and_poll's error
            # branch will have published an event but update_status here is a
            # no-op against the cancelled terminal state — and we also skip the
            # success event so the SSE consumer doesn't see committed → completed
            # for a job they cancelled.
            post = await store.get(job.id)
            if post and post.status == JobStatus.cancelled:
                logger.info(
                    "run_chat_job: %s cancelled mid-poll; skipping success emit",
                    job.id,
                )
                return
            await store.update_status(
                job.id,
                JobStatus.succeeded,
                picked_title=outcome.title,
                picked_author=outcome.author,
            )
            await bus.emit_async("completed", {"title": outcome.title, "author": outcome.author})
        else:
            # asked / no_results / error — agent already published progress events.
            msg = outcome.message or f"agent ended: {outcome.kind}"
            await store.update_status(job.id, JobStatus.failed, error=msg)
            if outcome.kind not in ("asked", "no_results") and not bus.messaged:
                await bus.emit_async("error", {"message": msg})

    except asyncio.CancelledError:
        logger.info("run_chat_job: cancelled (likely SIGTERM) for %s", job_id)
        await store.update_status(job.id, JobStatus.failed, error="worker cancelled")
        try:
            await bus.emit_async("error", {"message": "worker cancelled"})
        except Exception:  # noqa: BLE001
            pass  # bus publish may also fail mid-shutdown — best effort
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("run_chat_job crashed for %s", job_id)
        await store.update_status(job.id, JobStatus.failed, error=f"{type(e).__name__}: {e}")
        await bus.emit_async("error", {"message": f"{type(e).__name__}: {e}"})


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
        log = EventLog(redis)
        ctx["redis"] = redis
        ctx["log"] = log
        ctx["store"] = JobStore(
            redis,
            log,
            state_ttl_s=settings.job_state_ttl_s,
            dedup_ttl_s=settings.job_dedup_ttl_s,
        )

    @staticmethod
    async def on_shutdown(ctx: dict[str, Any]) -> None:
        await ctx["redis"].aclose()
