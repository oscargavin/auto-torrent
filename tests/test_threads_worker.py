"""The turn's branching, which is the heart of the conversational layer.

The invariant under test throughout: **a job exists only if something is
downloading.** A turn that ends in a question, or in "couldn't find it", must
leave no job behind — otherwise the app shows a progress card with no honest
status in it, and the reaper eventually fails a job that never started.
"""

import asyncio

import pytest

from auto_torrent.server.agent import AgentOutcome
from auto_torrent.types import BookCard
from auto_torrent.server.jobs.events import EventLog
from auto_torrent.server.jobs.store import JobStore
from auto_torrent.server.threads import worker as worker_mod
from auto_torrent.server.threads.store import ThreadStore
from auto_torrent.server.threads.types import Message, MessageKind, ThreadStatus


@pytest.fixture
def ctx(redis):
    log = EventLog(redis)
    thread_log = EventLog(redis, prefix="thread")
    return {
        "log": log,
        "thread_log": thread_log,
        "store": JobStore(redis, log, state_ttl_s=3600, dedup_ttl_s=600),
        "threads": ThreadStore(redis, thread_log, state_ttl_s=3600),
    }


def _agent_returning(outcome: AgentOutcome, *, calls: list | None = None, kind: str = "edition"):
    async def fake_run_agent(raw_query, phone, settings, sms, **kw):
        if calls is not None:
            calls.append({"query": raw_query, **kw})
        if outcome.kind == "asked":
            # The real tool calls on_ask before returning; mirror that so the
            # choice message is written the way production writes it.
            await kw["on_ask"](outcome.message or "Which one?", kind, outcome.options)
        return outcome

    return fake_run_agent


async def test_question_parks_the_thread_and_creates_no_job(ctx, monkeypatch, redis):
    options = [
        {"title": "Dune", "narrator": "Simon Vance", "magnet": "magnet:?xt=a"},
        {"title": "Dune", "narrator": "Scott Brick", "magnet": "magnet:?xt=b"},
    ]
    monkeypatch.setattr(
        worker_mod,
        "run_agent",
        _agent_returning(
            AgentOutcome(
                kind="asked", options=options, message="Two narrators — which?"
            )
        ),
    )
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "dune")

    assert (await ctx["threads"].get(thread.id)).status is ThreadStatus.awaiting_choice
    msgs = await ctx["threads"].messages(thread.id)
    assert [m.kind for m in msgs] == [MessageKind.choice]
    assert len(msgs[0].options) == 2
    # The question rides on the choice message rather than arriving as a
    # separate assistant line — observed live, the agent skipped the preamble
    # entirely and left a bare list of near-identical rows.
    assert msgs[0].text == "Two narrators — which?"
    # No job — nothing is downloading.
    assert await ctx["store"].list_for_profile("p1") == []
    # Magnets stayed server-side.
    assert "magnet" not in msgs[0].model_dump_json()
    assert (await ctx["threads"].take_pending(thread.id))[1]["magnet"] == "magnet:?xt=b"


async def test_no_results_says_something_and_creates_no_job(ctx, monkeypatch):
    monkeypatch.setattr(
        worker_mod,
        "run_agent",
        _agent_returning(AgentOutcome(kind="error", message="Couldn't find that one.")),
    )
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "asdfghjkl")

    msgs = await ctx["threads"].messages(thread.id)
    assert [m.kind for m in msgs] == [MessageKind.assistant]
    assert msgs[0].text == "Couldn't find that one."
    assert (await ctx["threads"].get(thread.id)).status is ThreadStatus.idle
    assert await ctx["store"].list_for_profile("p1") == []


async def test_commit_creates_a_job_and_links_it_into_the_thread(ctx, monkeypatch):
    finished: dict = {}

    async def fake_finish(*, store, bus, job, outcome):
        finished["job_id"] = job.id
        finished["bound"] = bus.job_id

    monkeypatch.setattr(worker_mod, "finish_agent_outcome", fake_finish)
    monkeypatch.setattr(
        worker_mod,
        "run_agent",
        _agent_returning(
            AgentOutcome(kind="committed", title="Dune", author="Frank Herbert")
        ),
    )
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "dune")

    msgs = await ctx["threads"].messages(thread.id)
    assert [m.kind for m in msgs] == [MessageKind.job]
    job_id = msgs[0].job_id
    assert job_id and (await ctx["store"].get(job_id)) is not None
    # The sink switched from thread-activity to the job's own stream, which is
    # what keeps the existing progress rail working unchanged.
    assert finished["bound"] == job_id == finished["job_id"]


async def test_history_excludes_the_message_being_answered(ctx, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        worker_mod,
        "run_agent",
        _agent_returning(AgentOutcome(kind="error", message="nope"), calls=calls),
    )
    thread = await ctx["threads"].create("p1")
    await ctx["threads"].append(
        thread.id, Message.new(thread.id, MessageKind.user, text="something funny")
    )
    await ctx["threads"].append(
        thread.id, Message.new(thread.id, MessageKind.assistant, text="How about X?")
    )
    await ctx["threads"].append(
        thread.id, Message.new(thread.id, MessageKind.user, text="shorter")
    )

    await worker_mod.run_thread_turn(ctx, thread.id, "shorter")

    history = calls[0]["history"]
    assert [role for role, _ in history] == ["user", "assistant"]
    assert "shorter" not in [text for _, text in history]


async def test_crash_unlocks_the_thread_and_says_so(ctx, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(worker_mod, "run_agent", boom)
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "dune")

    # Both obligations: a readable reply, and a composer that isn't stuck.
    msgs = await ctx["threads"].messages(thread.id)
    assert msgs and msgs[-1].kind is MessageKind.assistant
    assert (await ctx["threads"].get(thread.id)).status is ThreadStatus.idle


async def test_suggested_books_get_cover_art(ctx, monkeypatch):
    """A "which book?" list is named from knowledge, not from a search, so it
    arrives with no artwork — four placeholder glyphs next to an edition list
    that has real covers."""
    monkeypatch.setattr(
        worker_mod,
        "find_card",
        lambda title, author: BookCard(
            title=title, author=author, cover_url=f"https://img/{title}.jpg",
            description="blurb", rating=4.6, rating_count=1200, runtime_min=610,
        ),
    )
    monkeypatch.setattr(
        worker_mod,
        "run_agent",
        _agent_returning(
            AgentOutcome(
                kind="asked",
                message="Which?",
                options=[{"title": "Elantris"}, {"title": "Mistborn"}],
            )
        ),
    )
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "something like sanderson")

    opts = (await ctx["threads"].messages(thread.id))[0].options
    assert [o.cover_url for o in opts] == [
        "https://img/Elantris.jpg",
        "https://img/Mistborn.jpg",
    ]


async def test_search_results_keep_their_own_cover(ctx, monkeypatch):
    """Editions already carry the scraper's art; re-looking it up would be a
    wasted round trip and could replace the right cover with a near-miss."""
    calls: list = []

    def _spy(title, author):
        calls.append(title)
        return BookCard(title=title, author=author, cover_url="https://img/other.jpg")

    monkeypatch.setattr(worker_mod, "find_card", _spy)
    monkeypatch.setattr(
        worker_mod,
        "run_agent",
        _agent_returning(
            AgentOutcome(
                kind="asked",
                message="Which?",
                options=[{"title": "Dune", "cover_url": "https://abb/dune.jpg"}],
            )
        ),
    )
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "dune")

    opts = (await ctx["threads"].messages(thread.id))[0].options
    assert opts[0].cover_url == "https://abb/dune.jpg"


async def test_a_failed_cover_lookup_never_costs_the_question(ctx, monkeypatch):
    def _boom(title, author):
        raise RuntimeError("audible down")

    monkeypatch.setattr(worker_mod, "find_card", _boom)
    monkeypatch.setattr(
        worker_mod,
        "run_agent",
        _agent_returning(
            AgentOutcome(kind="asked", message="Which?", options=[{"title": "Elantris"}])
        ),
    )
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "sanderson")

    msgs = await ctx["threads"].messages(thread.id)
    assert msgs[0].kind is MessageKind.choice
    assert msgs[0].options[0].cover_url == ""
    assert (await ctx["threads"].get(thread.id)).status is ThreadStatus.awaiting_choice


async def test_a_question_is_answered_not_searched_for(ctx, monkeypatch):
    """Observed live: "whats piranesi actually about?" came back as "Couldn't
    find that one — try the full title and author." Ending without committing
    fell through to the not-found fallback, so a good question got a nonsense
    answer about a book the agent had just recommended."""
    async def fake_run_agent(raw_query, phone, settings, sms, **kw):
        await asyncio.to_thread(sms.send, phone, "A man in an endless house of statues.")
        return AgentOutcome(kind="replied", message="A man in an endless house of statues.")

    monkeypatch.setattr(worker_mod, "run_agent", fake_run_agent)
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "whats piranesi about?")

    msgs = await ctx["threads"].messages(thread.id)
    assert [m.kind for m in msgs] == [MessageKind.assistant]
    assert msgs[0].text == "A man in an endless house of statues."
    # No download, no question, composer handed straight back.
    assert await ctx["store"].list_for_profile("p1") == []
    assert (await ctx["threads"].get(thread.id)).status is ThreadStatus.idle


async def test_tool_calls_persist_as_a_group_above_the_reply(ctx, monkeypatch):
    """The old activity row replaced each line with the next, so by the time
    the agent spoke the whole chain was gone. A conversation has to still show
    what it did tomorrow."""
    async def fake_run_agent(raw_query, phone, settings, sms, **kw):
        await sms.emit_async("progress", {"stage": "searching", "text": "Searching…"})
        await sms.emit_async("progress", {"stage": "searching", "text": "Found 8 copies…"})
        await asyncio.to_thread(sms.send, phone, "Found it. Downloading now.")
        return AgentOutcome(kind="replied", message="Found it. Downloading now.")

    monkeypatch.setattr(worker_mod, "run_agent", fake_run_agent)
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "dune")

    msgs = await ctx["threads"].messages(thread.id)
    assert [m.kind for m in msgs] == [MessageKind.steps, MessageKind.assistant]
    assert [s.text for s in msgs[0].steps] == ["Searching…", "Found 8 copies…"]


async def test_a_repeated_narration_is_one_step(ctx, monkeypatch):
    """The agent calls a tool as many times as it likes — probing three magnets
    emitted the same line three times, which reads as a stuck list."""
    async def fake_run_agent(raw_query, phone, settings, sms, **kw):
        for _ in range(3):
            await sms.emit_async("progress", {"text": "Checking who's sharing it…"})
        await asyncio.to_thread(sms.send, phone, "Done.")
        return AgentOutcome(kind="replied", message="Done.")

    monkeypatch.setattr(worker_mod, "run_agent", fake_run_agent)
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "dune")

    steps = (await ctx["threads"].messages(thread.id))[0].steps
    assert [s.text for s in steps] == ["Checking who's sharing it…"]


async def test_text_between_tool_calls_starts_a_new_group(ctx, monkeypatch):
    """The chain-of-thought boundary rule: assistant text breaks the run."""
    async def fake_run_agent(raw_query, phone, settings, sms, **kw):
        await sms.emit_async("progress", {"text": "Searching…"})
        await asyncio.to_thread(sms.send, phone, "Two versions of this one.")
        await sms.emit_async("progress", {"text": "Checking the covers…"})
        await asyncio.to_thread(sms.send, phone, "Got it.")
        return AgentOutcome(kind="replied", message="Got it.")

    monkeypatch.setattr(worker_mod, "run_agent", fake_run_agent)
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "dune")

    msgs = await ctx["threads"].messages(thread.id)
    assert [m.kind for m in msgs] == [
        MessageKind.steps,
        MessageKind.assistant,
        MessageKind.steps,
        MessageKind.assistant,
    ]
    assert [s.text for s in msgs[0].steps] == ["Searching…"]
    assert [s.text for s in msgs[2].steps] == ["Checking the covers…"]


async def test_steps_land_above_the_options_they_produced(ctx, monkeypatch):
    async def fake_run_agent(raw_query, phone, settings, sms, **kw):
        await sms.emit_async("progress", {"text": "Searching…"})
        await kw["on_ask"]("Which?", "edition", [{"title": "A"}, {"title": "B"}])
        return AgentOutcome(kind="asked", message="Which?", options=[{"title": "A"}])

    monkeypatch.setattr(worker_mod, "find_card", lambda t, a: None)
    monkeypatch.setattr(worker_mod, "run_agent", fake_run_agent)
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "dune")

    msgs = await ctx["threads"].messages(thread.id)
    assert [m.kind for m in msgs] == [MessageKind.steps, MessageKind.choice]


async def test_a_crash_still_shows_how_far_it_got(ctx, monkeypatch):
    async def fake_run_agent(raw_query, phone, settings, sms, **kw):
        await sms.emit_async("progress", {"text": "Searching…"})
        raise RuntimeError("boom")

    monkeypatch.setattr(worker_mod, "run_agent", fake_run_agent)
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "dune")

    msgs = await ctx["threads"].messages(thread.id)
    assert msgs[0].kind is MessageKind.steps
    assert [s.text for s in msgs[0].steps] == ["Searching…"]


async def test_options_carry_what_the_expanded_view_needs(ctx, monkeypatch):
    """A row shows a line or two; the rating, blurb and runtime someone
    actually decides on live behind the disclosure."""
    monkeypatch.setattr(
        worker_mod,
        "find_card",
        lambda title, author: BookCard(
            title=title, author="Susanna Clarke", cover_url="https://img/p.jpg",
            description="A man in an endless house.", rating=4.4,
            rating_count=9100, runtime_min=395, year=2020,
        ),
    )
    monkeypatch.setattr(
        worker_mod,
        "run_agent",
        _agent_returning(
            AgentOutcome(kind="asked", message="Which?", options=[{"title": "Piranesi"}])
        ),
    )
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "something strange")

    opt = (await ctx["threads"].messages(thread.id))[0].options[0]
    assert opt.rating == 4.4 and opt.rating_count == 9100
    assert opt.description == "A man in an endless house."
    assert opt.runtime_min == 395 and opt.year == 2020
    # Goodreads retired its API, so this is a search link — no id to resolve.
    assert opt.goodreads_url.startswith("https://www.goodreads.com/search?q=Piranesi")


async def test_the_download_card_gets_the_cover_they_chose(ctx, monkeypatch):
    """Previously the download showed a generic stage glyph even though the row
    the user tapped had artwork right there."""
    async def fake_finish(*, store, bus, job, outcome):
        pass

    monkeypatch.setattr(worker_mod, "finish_agent_outcome", fake_finish)
    monkeypatch.setattr(
        worker_mod,
        "run_agent",
        _agent_returning(AgentOutcome(kind="committed", title="Dune", author="Frank Herbert")),
    )
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(
        ctx, thread.id, "Download Dune", pending_options=[{"cover_url": "https://img/dune.jpg"}]
    )

    job_id = (await ctx["threads"].messages(thread.id))[0].job_id
    assert (await ctx["store"].get(job_id)).cover_url == "https://img/dune.jpg"


async def test_a_download_with_no_chosen_row_still_finds_a_cover(ctx, monkeypatch):
    async def fake_finish(*, store, bus, job, outcome):
        pass

    monkeypatch.setattr(worker_mod, "finish_agent_outcome", fake_finish)
    monkeypatch.setattr(
        worker_mod, "find_cover_url", lambda title, author: "https://img/looked-up.jpg"
    )
    monkeypatch.setattr(
        worker_mod,
        "run_agent",
        _agent_returning(AgentOutcome(kind="committed", title="Dune", author="Frank Herbert")),
    )
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "dune")

    job_id = (await ctx["threads"].messages(thread.id))[0].job_id
    assert (await ctx["store"].get(job_id)).cover_url == "https://img/looked-up.jpg"


async def test_one_lookup_per_book_not_per_option(ctx, monkeypatch):
    """A "which edition?" question is N rows of the SAME book. Each lookup is an
    Audible search plus an Audnexus fetch, so per-option meant 8 HTTP calls for
    one book's worth of answer — with the user waiting on it."""
    calls: list[tuple[str, str]] = []

    def _spy(title, author):
        calls.append((title, author))
        return BookCard(title=title, author=author, rating=4.5)

    monkeypatch.setattr(worker_mod, "find_card", _spy)
    monkeypatch.setattr(
        worker_mod,
        "run_agent",
        _agent_returning(
            AgentOutcome(
                kind="asked",
                message="Which narrator?",
                options=[
                    {"title": "Dune", "author": "Frank Herbert", "narrator": "Simon Vance"},
                    {"title": "Dune", "author": "Frank Herbert", "narrator": "Scott Brick"},
                    {"title": "dune", "author": "frank herbert", "narrator": "Euan Morton"},
                ],
            )
        ),
    )
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "dune")

    assert calls == [("Dune", "Frank Herbert")]
    # Every row still gets the answer.
    opts = (await ctx["threads"].messages(thread.id))[0].options
    assert [o.rating for o in opts] == [4.5, 4.5, 4.5]
