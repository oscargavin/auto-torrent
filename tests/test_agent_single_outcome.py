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
    monkeypatch.setattr(agent_mod, "query", fake_query)
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
