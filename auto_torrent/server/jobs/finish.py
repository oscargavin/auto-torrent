"""Everything that happens after the agent stops talking.

Its own module rather than a function in `jobs/worker.py` because both workers
need it: the bare jobs path calls it with the job it started from, and the
conversational path calls it with the job it creates once the agent commits.
Sharing it from `jobs/worker.py` meant `threads/worker.py` importing from a
module that imports `run_thread_turn` back — a cycle that had to be broken with
a module-bottom import and a function-body import, neither of which a linter can
check and both of which a test then had to explain.

This is the half holding the cancel races, the library check and the poll
budget: exactly the code that must not be forked.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..agent import AgentOutcome
from ..app import _emit_download_and_poll  # re-uses the existing pump
from ..audiobookshelf import ABSClient
from ..library_match import find_existing
from ..llm import clear_conversation
from ..settings import Settings
from ..worker import JOB_BUDGET_S, _kill_download_and_clean
from ...cli import _read_state
from .store import JobStore
from .types import FailureClass, Job, JobStatus

logger = logging.getLogger("atb.jobs.finish")
settings = Settings()


def as_failure_class(raw: str | None) -> FailureClass:
    """Map the download layer's string onto the wire enum.

    The download layer carries plain strings on its exception classes so it
    doesn't have to import the jobs vocabulary. Anything unrecognised becomes
    infra_error rather than raising — a classification bug must not turn into
    a second failure while we're already handling the first.
    """
    try:
        return FailureClass(raw or "")
    except ValueError:
        logger.warning("unmapped failure_class %r, recording as infra_error", raw)
        return FailureClass.infra_error


async def already_in_library(title: str, author: str) -> bool:
    """Is this book already on the shelf?

    Never raises: a library that can't be reached must not fail an otherwise
    good download. The cost of the check being unavailable is a duplicate, and
    the cost of it throwing is losing the book entirely.
    """
    if not title:
        return False
    try:
        items = await ABSClient(settings).list_items(settings.abs_library_id)
    except Exception:  # noqa: BLE001
        logger.warning("duplicate check skipped: library list failed", exc_info=True)
        return False
    return find_existing(items, title, author) is not None


async def finish_agent_outcome(
    *, store: JobStore, bus: Any, job: Job, outcome: AgentOutcome
) -> None:
    """Record what the agent decided, and see the download through if it committed."""
    if outcome.kind != "committed":
        # asked / no_results / error — the agent has already published its own
        # progress narration. The terminal event comes from update_status, not
        # from here: the old guard also required `not bus.messaged`, and
        # bus.messaged is set by the mandatory opening "Searching…" frame, so in
        # practice it suppressed the terminal event for *every* non-committed
        # outcome and the client spun forever.
        await store.update_status(
            job.id,
            JobStatus.failed,
            error=outcome.message or f"agent ended: {outcome.kind}",
            failure_class=FailureClass.not_found,
        )
        return

    clear_conversation(job.id)
    await store.set_picked_book(
        job.id,
        title=outcome.title,
        author=outcome.author,
        narrator=outcome.narrator,
        file_format=outcome.file_format,
    )
    # The agent has already spawned the download subprocess; register its
    # state-file id against the job so a subsequent DELETE can find the running
    # PID + landing path and tear them down.
    download_id = (outcome.download or {}).get("id")
    if download_id:
        await store.set_download_id(job.id, download_id)

    # Only now do we know which book this actually is: the agent resolves
    # "something like Project Hail Mary" into a title, and the user's own words
    # may match nothing in the library while the resolved book is already
    # sitting on the shelf. Checking here also covers requests that never went
    # through the app at all.
    if await already_in_library(outcome.title, outcome.author):
        logger.info(
            "%s resolved to %r, already in library — not downloading",
            job.id,
            outcome.title,
        )
        # The agent has already spawned the download; stop it and clean the
        # partial, exactly as a user cancel would.
        _kill_if_running(download_id)
        await store.update_status(
            job.id,
            JobStatus.succeeded,
            picked_title=outcome.title,
            picked_author=outcome.author,
            already_had=True,
        )
        return

    # Re-check status: cancel may have fired during the agent's search (which
    # can take 10–30s). If so, skip the poll — _emit_download would otherwise
    # loop on the subprocess that cancel_job is about to kill (or already
    # killed), producing a confusing extra error event after the user already
    # saw cancelled.
    current = await store.get(job.id)
    if current and current.status == JobStatus.cancelled:
        logger.info("%s cancelled during agent run; not entering poll", job.id)
        # Race close-out: cancel_job's DELETE handler may have read download_id
        # as None (set_download_id above hadn't landed yet) and skipped the
        # kill. We just registered it, so we own the orphan subprocess.
        await _kill_if_running(download_id)
        return

    # Keep the store's download_id current as poll_and_finalise swaps to
    # fallback magnets on stall — otherwise cancel would kill (or try to kill)
    # the dead original instead of the running fallback, and the fallback would
    # land in the library against the user's cancel intent.
    async def _track_download_change(new_id: str) -> None:
        await store.set_download_id(job.id, new_id)

    # `ok` is true only if the book actually landed in the library; otherwise
    # `failure_class` says why. Every abandon path — no candidates left, unknown
    # poll outcome, lost state file, failed import — comes back here as a
    # failure rather than sliding through as success.
    result = await _emit_download_and_poll(
        bus,
        download=outcome.download or {},
        fallbacks=outcome.fallbacks,
        display=outcome.display,
        title=outcome.title,
        author=outcome.author,
        session=job.id,
        on_download_change=_track_download_change,
        query=job.query,
        # JobStore.update_status is the sole producer of terminal events on the
        # jobs path — see U1 in the plan.
        emit_terminal=False,
        # One budget for the whole job: attempts, grace extensions and fallback
        # swaps all draw from it, so arq's timeout stays a backstop instead of
        # the thing that actually ends the job.
        deadline=time.monotonic() + JOB_BUDGET_S,
    )

    # If cancel fired during the poll, update_status here is a no-op against the
    # cancelled terminal state — and we skip the success status so the SSE
    # consumer doesn't see a cancelled job succeed.
    post = await store.get(job.id)
    if post and post.status == JobStatus.cancelled:
        logger.info("%s cancelled mid-poll; skipping success emit", job.id)
        return

    if result.ok:
        await store.update_status(
            job.id,
            JobStatus.succeeded,
            picked_title=outcome.title,
            picked_author=outcome.author,
        )
    else:
        await store.update_status(
            job.id,
            JobStatus.failed,
            error=result.message or "the download didn't finish",
            picked_title=outcome.title,
            picked_author=outcome.author,
            failure_class=as_failure_class(result.failure_class),
        )


async def _kill_if_running(download_id: str | None) -> None:
    """Tear down a spawned download, if there is one. The two abandon paths
    above did this identically."""
    if not download_id:
        return
    state = _read_state(download_id)
    if state:
        await _kill_download_and_clean(state)
