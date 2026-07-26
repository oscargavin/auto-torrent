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
from urllib.parse import quote_plus

from ...audnex import hydrate
from ..agent import run_agent
from ..jobs.store import JobStore
from ..settings import Settings
from .bus import ThreadSink
from .history import render_history
from .store import ThreadStore, option_from_payload
from .types import EVENT_ERROR, ChoiceOption, Message, MessageKind, ThreadStatus

logger = logging.getLogger("atb.threads.worker")
settings = Settings()

# What the agent is told the user said when they tap an option, in place of the
# text they never typed. Phrased as the user so the transcript reads as a
# conversation rather than as a protocol.
CHOICE_ECHO = "That one: {title}"


def goodreads_search_url(title: str, author: str) -> str:
    """Where to send someone who wants the reviews.

    A search link, not a book link: Goodreads retired its public API in 2020
    and issues no new keys, so there is no way to resolve a title to its work
    id. The search page lands on the right book for anything well known, which
    is every book that reaches this list.
    """
    query = quote_plus(" ".join(p for p in (title, author) if p).strip())
    return f"https://www.goodreads.com/search?q={query}" if query else ""


async def _hydrate(options: list[ChoiceOption]) -> list[ChoiceOption]:
    """Fill in everything the expanded view needs, from one lookup per option.

    A row can only show a line or two before it stops being scannable, so the
    description, rating and runtime that someone actually decides on live
    behind a disclosure — and none of that exists on a "which book?" option,
    which the agent named from what it knows rather than from a search result.

    Uses the same Audible-then-OpenLibrary pipeline as the recommendations
    shelf. Concurrent because this runs while the user waits on the question:
    four sequential lookups would be four times the delay. Guarded throughout —
    a lookup that fails costs that row its extra detail, never the question.
    """
    if not options:
        return options
    try:
        cards = await asyncio.gather(
            *(asyncio.to_thread(_lookup, o.title, o.author) for o in options),
            return_exceptions=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("option hydration failed")
        return options

    out: list[ChoiceOption] = []
    for option, card in zip(options, cards):
        patch: dict = {"goodreads_url": goodreads_search_url(option.title, option.author)}
        if card is not None and not isinstance(card, BaseException):
            # Never overwrite what the search already knew: an edition's own
            # cover is of the actual release, and its author came off the
            # result. Audible fills the gaps, it doesn't correct them.
            if not option.cover_url and card.cover_url:
                patch["cover_url"] = card.cover_url
            if not option.author and card.author:
                patch["author"] = card.author
            patch.update(
                description=card.description or "",
                rating=card.rating,
                rating_count=card.rating_count,
                runtime_min=card.runtime_min,
                year=card.year,
            )
        out.append(option.model_copy(update=patch))
    return out


def _cover_for(title: str, author: str) -> str:
    card = _lookup(title, author)
    return (card.cover_url or "") if card else ""


def _lookup(title: str, author: str):
    """Audible/Audnexus card for a title, or None. Never raises."""
    try:
        return hydrate(title, author)
    except Exception:  # noqa: BLE001
        logger.warning("hydrate failed for %r", title, exc_info=True)
        return None


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
        hydrated = await _hydrate(
            [option_from_payload(i, o) for i, o in enumerate(options)]
        )
        # Carry the artwork back into the pending payload, so choosing a
        # suggested book doesn't need a second identical lookup just to put a
        # cover on its download card.
        for raw, option in zip(options, hydrated):
            if option.cover_url and not raw.get("cover_url"):
                raw["cover_url"] = option.cover_url
        await threads.set_pending(thread_id, options)
        # The run of tool calls that produced these options belongs above them.
        await sink.drain()
        await sink.flush_steps()
        await threads.append(
            thread_id,
            Message.new(
                thread_id, MessageKind.choice, text=question, options=hydrated
            ),
        )

    # The option the user tapped, if this turn is answering a question.
    pending_cover = (
        str((pending_options or [{}])[0].get("cover_url") or "").strip()
        if pending_options
        else ""
    )

    try:
        history = render_history(
            await threads.history_for_agent(thread_id),
            # The message just posted is the request itself, not history.
            exclude_last_text=text,
        )

        outcome = await run_agent(
            text,
            thread_id,
            settings,
            sink,
            pending_options=pending_options or None,
            on_ask=on_ask,
            history=history or None,
        )

        if outcome.kind == "replied":
            # The answer already landed as an assistant message. Nothing to
            # download, nothing to ask — just hand the composer back.
            await threads.set_status(thread_id, ThreadStatus.idle)
            return

        if outcome.kind == "asked":
            # on_ask already wrote the message. Park the thread; the worker slot
            # is released by returning, which is the whole reason the agent is
            # re-entered on answer rather than blocked here waiting for one.
            await threads.set_status(thread_id, ThreadStatus.awaiting_choice)
            return

        if outcome.kind == "committed":
            await threads.clear_pending(thread_id)
            await sink.drain()
            await sink.flush_steps()
            job = await jobs.create_direct(thread.profile_id, outcome.title or text)
            # The cover is already known in the common case — the user tapped a
            # row that had one — so the download card shows the same book they
            # chose rather than a generic glyph. Falling back to a lookup keeps
            # it true for a request that never went through a choice.
            cover = pending_cover or await asyncio.to_thread(
                _cover_for, outcome.title, outcome.author
            )
            if cover:
                await jobs.set_cover(job.id, cover)
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
        await sink.drain()
        await sink.flush_steps()
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
        # Whatever it managed before dying is still worth showing — it is often
        # the only clue about where it got to.
        await sink.drain()
        await sink.flush_steps()
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
