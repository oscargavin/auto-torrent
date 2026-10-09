"""One turn, one outcome.

Observed live: the agent asked which edition of Philosopher's Stone, then
committed a download anyway. The user was shown a question that had already
been answered for them, and a book nobody chose landed in a shared library.

The cause is that the SDK loop `continue`s once an outcome is set rather than
stopping, so nothing stopped a second terminal tool from firing. The tools
themselves have to refuse.
"""

import pytest

from auto_torrent.server import agent as agent_mod

from tests.sdk_fake import fake_client


class _Sink:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, _phone, text):
        self.sent.append(text)

    def emit(self, *_a, **_kw):
        pass


@pytest.fixture
async def tools(monkeypatch):
    """The agent's tool handlers, captured where they are registered.

    They are closures over run_agent's `state`, so this is the only way to
    exercise the guard against the state it actually protects.
    """
    captured: dict = {}

    def fake_server(name, tools):
        captured.update({t.name: t.handler for t in tools})
        return object()

    async def fake_query(prompt, options):
        if False:
            yield None

    monkeypatch.setattr(agent_mod, "create_sdk_mcp_server", fake_server)
    monkeypatch.setattr(agent_mod, "ClaudeSDKClient", fake_client(fake_query))
    monkeypatch.setattr(agent_mod, "_execute_download_bg", lambda *a, **kw: {"id": "dl1"})

    asked: list = []

    async def on_ask(question, kind, options):
        asked.append((question, kind, options))

    await agent_mod.run_agent("harry potter", "t1", agent_mod.Settings(), _Sink(), on_ask=on_ask)
    captured["_asked"] = asked
    return captured


async def test_a_commit_after_an_ask_is_refused(tools):
    """The exact live failure."""
    ask = await tools["ask_user_to_pick"](
        {"kind": "edition", "question": "Which?", "options": [{"title": "HP", "magnet": "m"}]}
    )
    assert "asked user" in ask["content"][0]["text"]
    assert len(tools["_asked"]) == 1

    commit = await tools["commit_download"](
        {"primary": {"magnet": "m", "title": "HP"}, "fallbacks": []}
    )
    assert "already ended" in commit["content"][0]["text"]


async def test_a_second_ask_is_refused(tools):
    first = await tools["ask_user_to_pick"](
        {"kind": "book", "question": "Which?", "options": [{"title": "A"}]}
    )
    assert "asked user" in first["content"][0]["text"]

    second = await tools["ask_user_to_pick"](
        {"kind": "book", "question": "Or these?", "options": [{"title": "B"}]}
    )
    assert "already ended" in second["content"][0]["text"]
    # The second question never reached the thread.
    assert len(tools["_asked"]) == 1


async def test_a_reply_after_a_commit_is_refused(tools):
    commit = await tools["commit_download"](
        {"primary": {"magnet": "m", "title": "HP"}, "fallbacks": []}
    )
    assert "started" in commit["content"][0]["text"]

    reply = await tools["reply"]({"text": "actually, here's a thought"})
    assert "already ended" in reply["content"][0]["text"]


async def test_the_first_terminal_call_still_works(tools):
    """The guard must not break the normal single-outcome path."""
    out = await tools["reply"]({"text": "Piranesi is about a house."})
    assert "replied" in out["content"][0]["text"]


async def test_running_out_of_turns_gets_its_own_answer(monkeypatch):
    """The SDK reports max turns as a result message, then raises the same bare
    "exit code 1" as a dead CLI. The user hears what to do differently."""
    from claude_agent_sdk import ResultMessage

    async def fake_query(prompt, options):
        yield ResultMessage(
            subtype="error_max_turns", duration_ms=1, duration_api_ms=1,
            is_error=True, num_turns=16, session_id="s",
        )
        raise Exception("Command failed with exit code 1 (exit code: 1)")

    monkeypatch.setattr(agent_mod, "create_sdk_mcp_server", lambda name, tools: object())
    monkeypatch.setattr(agent_mod, "ClaudeSDKClient", fake_client(fake_query))

    outcome = await agent_mod.run_agent("dune", "t1", agent_mod.Settings(), _Sink())

    assert outcome.kind == "error"
    assert outcome.crashed
    assert "exact title" in outcome.message
