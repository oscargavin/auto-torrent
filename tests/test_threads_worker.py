"""The turn's branching, which is the heart of the conversational layer.

The invariant under test throughout: **a job exists only if something is
downloading.** A turn that ends in a question, or in "couldn't find it", must
leave no job behind — otherwise the app shows a progress card with no honest
status in it, and the reaper eventually fails a job that never started.
"""

import pytest

from auto_torrent.server.agent import AgentOutcome
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


def _agent_returning(outcome: AgentOutcome, *, calls: list | None = None):
    async def fake_run_agent(raw_query, phone, settings, sms, **kw):
        if calls is not None:
            calls.append({"query": raw_query, **kw})
        if outcome.kind == "asked":
            # The real tool calls on_ask before returning; mirror that so the
            # choice message is written the way production writes it.
            await kw["on_ask"](outcome.options)
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
        _agent_returning(AgentOutcome(kind="asked", options=options)),
    )
    thread = await ctx["threads"].create("p1")

    await worker_mod.run_thread_turn(ctx, thread.id, "dune")

    assert (await ctx["threads"].get(thread.id)).status is ThreadStatus.awaiting_choice
    msgs = await ctx["threads"].messages(thread.id)
    assert [m.kind for m in msgs] == [MessageKind.choice]
    assert len(msgs[0].options) == 2
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

    # Patched at the source module: threads.worker imports it inside the
    # function to break the circular registration, so it resolves at call time.
    from auto_torrent.server.jobs import worker as jobs_worker

    monkeypatch.setattr(jobs_worker, "finish_agent_outcome", fake_finish)
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
