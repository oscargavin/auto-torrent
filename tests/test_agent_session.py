"""An agent session stays open between the turns of one conversation."""

from unittest.mock import patch

import pytest

from auto_torrent.server import agent as agent_module
from auto_torrent.server.threads import worker as worker_mod

from tests.sdk_fake import fake_client, one_turn


class _Bus:
    def emit(self, *_a, **_kw):
        pass

    def send(self, *_a, **_kw):
        pass


async def _no_ask(*_a):
    pass


async def _silent(prompt, options):
    return
    yield


async def test_one_cli_serves_every_turn_and_history_goes_in_once():
    client = fake_client(_silent)
    with patch.object(agent_module, "ClaudeSDKClient", client):
        session = agent_module.AgentSession()
        history = [("user", "something like Dune"), ("assistant", "Try Hyperion.")]
        for text in ("dune", "the second one"):
            await one_turn(
                text, "t1", object(), _Bus(), on_ask=_no_ask, history=history, session=session
            )
        await session.close()

    assert len(client.instances) == 1
    first, second = client.instances[0].prompts
    assert "Earlier in this conversation" in first
    # The session already has the conversation; sending it again would
    # double it in the context.
    assert "Earlier in this conversation" not in second
    assert "the second one" in second


async def test_a_one_off_request_closes_its_session():
    client = fake_client(_silent)
    with patch.object(agent_module, "ClaudeSDKClient", client):
        await one_turn("dune", "t1", object(), _Bus(), on_ask=_no_ask)
    assert client.instances[0].connected is False


async def test_a_failed_turn_marks_the_session_broken():
    async def boom(prompt, options):
        raise RuntimeError("Command failed with exit code 1")
        yield

    with patch.object(agent_module, "ClaudeSDKClient", fake_client(boom)):
        session = agent_module.AgentSession()
        outcome = await one_turn(
            "dune", "t1", object(), _Bus(), on_ask=_no_ask, session=session
        )
    assert outcome.kind == "error"
    assert session.broken


@pytest.fixture
def pool(monkeypatch):
    worker_mod._sessions.clear()
    monkeypatch.setattr(agent_module, "ClaudeSDKClient", fake_client(_silent))
    yield worker_mod._sessions
    worker_mod._sessions.clear()


async def test_the_pool_reuses_a_conversations_session(pool):
    a = await worker_mod._session_for("t1")
    assert await worker_mod._session_for("t1") is a
    assert await worker_mod._session_for("t2") is not a


async def test_the_pool_replaces_a_broken_or_long_session(pool):
    a = await worker_mod._session_for("t1")
    a.broken = True
    b = await worker_mod._session_for("t1")
    assert b is not a
    b.turns = agent_module.SESSION_MAX_TURNS
    assert await worker_mod._session_for("t1") is not b


async def test_the_pool_closes_idle_sessions_and_keeps_only_a_few(pool):
    for i in range(worker_mod.SESSION_MAX + 2):
        await worker_mod._session_for(f"t{i}")
    assert len(pool) == worker_mod.SESSION_MAX

    for session in pool.values():
        session.last_used -= worker_mod.SESSION_IDLE_S + 1
    await worker_mod.close_idle_sessions()
    assert pool == {}


async def test_two_turns_on_one_session_run_one_after_the_other():
    import asyncio

    order: list = []

    async def slow(prompt, options):
        order.append(("start", prompt.split("'")[1]))
        await asyncio.sleep(0.05)
        order.append(("end", prompt.split("'")[1]))
        return
        yield

    with patch.object(agent_module, "ClaudeSDKClient", fake_client(slow)):
        session = agent_module.AgentSession()
        await asyncio.gather(
            one_turn("one", "t1", object(), _Bus(), on_ask=_no_ask, session=session),
            one_turn("two", "t1", object(), _Bus(), on_ask=_no_ask, session=session),
        )
    assert order == [("start", "one"), ("end", "one"), ("start", "two"), ("end", "two")]


async def test_an_event_is_not_presented_as_the_user_speaking():
    client = fake_client(_silent)
    with patch.object(agent_module, "ClaudeSDKClient", client):
        await one_turn("", "t1", object(), _Bus(), event="Dune just appeared on the site.")
    prompt = client.instances[0].prompts[0]
    assert "Something happened (this is not the user speaking): Dune just appeared" in prompt
    assert "User said" not in prompt


async def test_an_open_session_hears_only_what_changed(redis):
    """A download that finished between turns reaches the next turn; the
    messages the session already saw do not come again."""
    from types import SimpleNamespace

    from auto_torrent.server.jobs.events import EventLog
    from auto_torrent.server.threads.store import ThreadStore
    from auto_torrent.server.threads.types import Message, MessageKind

    threads = ThreadStore(redis, EventLog(redis, prefix="thread"), state_ttl_s=3600)
    job = SimpleNamespace(
        id="j1", query="dune", picked_title="Dune", picked_narrator="", picked_format="",
        status="running", already_had=False, error=None,
    )

    class Jobs:
        async def get(self, job_id):
            return job if job_id == "j1" else None

    t = await threads.create("p1")
    await threads.append(t.id, Message.new(t.id, MessageKind.user, text="dune"))
    await threads.append(t.id, Message.new(t.id, MessageKind.job, job_id="j1"))
    session = agent_module.AgentSession()
    session.turns = 1
    await worker_mod._remember(session, threads, Jobs(), t.id)

    await threads.append(t.id, Message.new(t.id, MessageKind.user, text="is it done?"))
    job.status = "succeeded"
    news = await worker_mod._history_for(session, threads, Jobs(), t.id, "is it done?")
    assert news == [("assistant", "[download] “Dune” — done, in the library")]
