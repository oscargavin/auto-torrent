"""One conversational turn.

The shape that matters: a job is created **only if the agent commits**. A turn
that ends in a question, or in "couldn't find that", downloads nothing — giving
it a job record would put a progress card on screen with no honest status to
put in it, and would leave a permanently non-terminal job for the reaper to
eventually fail. Until something commits, the turn is just the thread being
`working`, which the client renders as a thinking row fed by the same narration
the progress rail used to carry.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..agent import run_agent
from ..covers import find_cover_url
from ..jobs.store import JobStore
from ..settings import Settings
from .bus import ThreadSink
from .store import ThreadStore, option_from_payload
from .types import EVENT_ERROR, ChoiceOption, Message, MessageKind, ThreadStatus

logger = logging.getLogger("atb.threads.worker")
settings = Settings()

# What the agent is told the user said when they tap an option, in place of the
# text they never typed. Phrased as the user so the transcript reads as a
# conversation rather than as a protocol.
CHOICE_ECHO = "That one: {title}"


async def _with_covers(options: list[ChoiceOption]) -> list[ChoiceOption]:
    """Fill in artwork for options that arrived without any.

    "Which edition?" options come from a search, so they carry the scraper's
    cover. "Which book?" options don't exist anywhere yet — the agent named
    them from what it knows rather than from a result — so four rows of
    placeholder glyphs is what a suggestion list would otherwise be, next to an
    edition list that has real covers.

    Uses the same Audible-then-OpenLibrary lookup as the recommendations shelf.
    Concurrent because this runs while the user waits on the question: four
    sequential lookups would be four times the delay for something decorative.
    `find_cover_url` swallows its own failures and returns None, and the gather
    is guarded anyway — a missing cover must never cost the question.
    """
    missing = [o for o in options if not o.cover_url]
    if not missing:
        return options
    try:
        found = await asyncio.gather(
            *(asyncio.to_thread(find_cover_url, o.title, o.author) for o in missing),
            return_exceptions=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("cover hydration failed")
        return options
    urls = {
        o.index: url
        for o, url in zip(missing, found)
        if isinstance(url, str) and url
    }
    return [
        o.model_copy(update={"cover_url": urls[o.index]}) if o.index in urls else o
        for o in options
    ]


async def run_thread_turn(
    ctx: dict[str, Any],
    thread_id: str,
    text: str,
    pending_options: list[dict] | None = None,
) -> None:
    # Imported here, not at module scope: jobs.worker registers this function on
    # WorkerSettings, so a top-level import in the other direction is circular.
    from ..jobs.worker import finish_agent_outcome

    threads: ThreadStore = ctx["threads"]
    jobs: JobStore = ctx["store"]
    log = ctx["log"]
    thread_log = ctx["thread_log"]

    thread = await threads.get(thread_id)
    if thread is None:
        logger.warning("run_thread_turn: missing thread %s", thread_id)
        return

    await threads.set_status(thread_id, ThreadStatus.working)
    sink = ThreadSink(
        log, thread_id=thread_id, thread_log=thread_log, threads=threads
    )

    async def on_ask(question: str, options: list[dict]) -> None:
        """Turn the agent's options into a choice message.

        The magnets stay in `set_pending`; only the presentable fields reach
        the transcript. Index is positional and is the contract between this
        message and the pending list — both are written here, together, so they
        cannot drift.
        """
        await threads.set_pending(thread_id, options)
        await threads.append(
            thread_id,
            Message.new(
                thread_id,
                MessageKind.choice,
                text=question,
                options=await _with_covers(
                    [option_from_payload(i, o) for i, o in enumerate(options)]
                ),
            ),
        )

    try:
        history = [
            ("user" if m.kind is MessageKind.user else "assistant", m.text)
            for m in await threads.history_for_agent(thread_id)
            # The message just posted is the request itself, not history.
            if m.text and m.text != text
        ]

        outcome = await run_agent(
            text,
            thread_id,
            settings,
            sink,
            pending_options=pending_options or None,
            on_ask=on_ask,
            history=history or None,
        )

        if outcome.kind == "asked":
            # on_ask already wrote the message. Park the thread; the worker slot
            # is released by returning, which is the whole reason the agent is
            # re-entered on answer rather than blocked here waiting for one.
            await threads.set_status(thread_id, ThreadStatus.awaiting_choice)
            return

        if outcome.kind == "committed":
            await threads.clear_pending(thread_id)
            job = await jobs.create_direct(thread.profile_id, outcome.title or text)
            await threads.append(
                thread_id,
                Message.new(thread_id, MessageKind.job, job_id=job.id),
            )
            # From here the download is a job like any other, and its progress
            # belongs on the job's own stream where the card is already
            # listening.
            sink.bind_job(job.id)
            await threads.set_status(thread_id, ThreadStatus.idle)
            await finish_agent_outcome(store=jobs, bus=sink, job=job, outcome=outcome)
            return

        # no_results / error. The agent usually said its piece through `send`
        # already; this is the backstop for when it didn't, so a turn can never
        # end in silence.
        if not sink.messaged:
            await threads.append(
                thread_id,
                Message.new(
                    thread_id,
                    MessageKind.assistant,
                    text=outcome.message or "I couldn't find that one.",
                ),
            )
        await threads.set_status(thread_id, ThreadStatus.idle)

    except Exception:  # noqa: BLE001
        logger.exception("run_thread_turn crashed for %s", thread_id)
        # Two separate obligations: tell the user (a message they can read and
        # reply to) and unlock the composer (status). Doing only the first
        # leaves them typing into a thread that will never answer.
        await thread_log.publish(
            thread_id, EVENT_ERROR, {"message": "Something went wrong on the server."}
        )
        await threads.append(
            thread_id,
            Message.new(
                thread_id,
                MessageKind.assistant,
                text="Something went wrong on my end. Try asking again?",
            ),
        )
        await threads.set_status(thread_id, ThreadStatus.idle)
