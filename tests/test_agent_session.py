"""An agent session stays open between the turns of one conversation."""

from unittest.mock import patch

import pytest

from auto_torrent.server import agent as agent_module
from auto_torrent.server.threads import worker as worker_mod

from tests.sdk_fake import fake_client


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
            await agent_module.run_agent(
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
        await agent_module.run_agent("dune", "t1", object(), _Bus(), on_ask=_no_ask)
    assert client.instances[0].connected is False


async def test_a_failed_turn_marks_the_session_broken():
    async def boom(prompt, options):
        raise RuntimeError("Command failed with exit code 1")
        yield

    with patch.object(agent_module, "ClaudeSDKClient", fake_client(boom)):
        session = agent_module.AgentSession()
        outcome = await agent_module.run_agent(
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
