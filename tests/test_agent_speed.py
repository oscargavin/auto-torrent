"""The agent-side speed fixes: each one removes waiting without changing what
the agent can find."""

from unittest.mock import patch

from auto_torrent.server import agent as agent_module
from auto_torrent.types import BookMetadata


class _Bus:
    def emit(self, *_a, **_kw):
        pass

    def send(self, *_a, **_kw):
        pass


async def _tools(**patches):
    captured: dict = {}

    def fake_server(name, tools):
        captured.update({t.name: t.handler for t in tools})
        return object()

    async def fake_query(prompt, options):
        captured["_options"] = options
        return
        yield

    with (
        patch.object(agent_module, "create_sdk_mcp_server", fake_server),
        patch.object(agent_module, "query", fake_query),
    ):
        await agent_module.run_agent("dune", "s1", object(), _Bus(), allow_ask=False)
    return captured


async def test_options_load_our_tools_up_front_and_drop_the_built_ins():
    options = (await _tools())["_options"]
    assert options.tools == []
    assert options.env["ENABLE_TOOL_SEARCH"] == "false"


async def test_a_repeated_search_is_answered_from_the_turns_cache():
    calls: list = []

    def fake_pipeline(q, limit, on_step=None, attempt=1, title="", author=""):
        calls.append(q)
        return {"book": {}, "results": [{"title": q}]}

    with patch.object(agent_module, "_search_pipeline_sync", fake_pipeline):
        tools = await _tools()
        first = await tools["search_audiobookbay"]({"query": "Dune", "limit": 5})
        again = await tools["search_audiobookbay"]({"query": "dune ", "limit": 5})
        other = await tools["search_audiobookbay"]({"query": "Dune Messiah", "limit": 5})

    assert calls == ["Dune", "Dune Messiah"]
    assert first == again
    assert other != first


async def test_probe_checks_every_candidate_in_one_call():
    seen: list = []

    def fake_probe(magnets, timeout, settle_s=None):
        seen.append((list(magnets), settle_s))
        return {m: 3 for m in magnets}

    with patch.object(agent_module, "_probe_seeds_batch", fake_probe):
        tools = await _tools()
        out = await tools["probe_peers"]({"magnets": ["m1", "m2", "m1"]})

    assert seen == [(["m1", "m2"], agent_module.PROBE_SETTLE_S)]
    assert '"m2": 3' in out["content"][0]["text"]


async def test_the_agents_title_wins_over_a_wrong_catalogue_guess(monkeypatch):
    """Observed live: "Dune Frank Herbert" looked up as Dune Messiah."""
    searched: list = []
    monkeypatch.setattr(
        agent_module, "lookup_book",
        lambda q: BookMetadata(title="Dune Messiah", author="Frank Herbert", series="American Science fiction"),
    )
    monkeypatch.setattr(
        agent_module, "_fan_out_search",
        lambda book, raw_query=None: searched.append(book) or [],
    )
    monkeypatch.setattr(agent_module, "get_proxy", lambda: None)

    data = agent_module._search_pipeline_sync(
        "Dune Frank Herbert", 5, title="Dune", author="Frank Herbert"
    )

    assert searched[0].title == "Dune"
    assert searched[0].series is None
    assert data["book"]["title"] == "Dune"


async def test_a_matching_catalogue_guess_keeps_its_series(monkeypatch):
    searched: list = []
    monkeypatch.setattr(
        agent_module, "lookup_book",
        lambda q: BookMetadata(title="Assassin's Apprentice", author="Robin Hobb", series="Farseer"),
    )
    monkeypatch.setattr(
        agent_module, "_fan_out_search",
        lambda book, raw_query=None: searched.append(book) or [],
    )
    monkeypatch.setattr(agent_module, "get_proxy", lambda: None)

    agent_module._search_pipeline_sync(
        "assassins apprentice", 5, title="Assassin’s Apprentice", author="Robin Hobb"
    )

    assert searched[0].series == "Farseer"


async def test_a_plain_text_answer_reaches_the_user():
    """Observed live: the agent answered "What is Piranesi about?" in text,
    without calling `reply`, and the user got "Couldn't find that one"."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    sent: list = []

    class Bus(_Bus):
        def send(self, _phone, text):
            sent.append(text)

    async def fake_query(prompt, options):
        yield AssistantMessage(content=[TextBlock(text="Piranesi is a strange, lovely novel.")], model="m")

    with (
        patch.object(agent_module, "create_sdk_mcp_server", lambda name, tools: object()),
        patch.object(agent_module, "query", fake_query),
    ):
        outcome = await agent_module.run_agent("what is piranesi about", "s1", object(), Bus())

    assert outcome.kind == "replied"
    assert sent == ["Piranesi is a strange, lovely novel."]
