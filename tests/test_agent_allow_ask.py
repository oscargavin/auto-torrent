"""U4: the jobs path can't carry an answer, so it must not generate a question.

These deliberately assert the WIRING, not the model's behaviour. Asserting
"the agent commits over an ambiguous fixture" would be asserting prompt
compliance — non-deterministic, and it either flakes or needs a live call.
"""
from unittest.mock import AsyncMock, patch

import pytest

from auto_torrent.server import agent as agent_module


async def _run(allow_ask: bool):
    """Run the agent with the SDK stubbed out, and return the options it built."""
    captured = {}

    def fake_server(name, tools):
        captured["tools"] = [getattr(t, "name", None) or t.__name__ for t in tools]
        return object()

    async def fake_query(prompt, options):
        captured["options"] = options
        return
        yield  # make it an async generator

    with (
        patch.object(agent_module, "create_sdk_mcp_server", fake_server),
        patch.object(agent_module, "query", fake_query),
    ):
        await agent_module.run_agent(
            "dune", "session-1", object(), AsyncMock(), allow_ask=allow_ask
        )
    return captured


async def test_jobs_path_has_no_ask_tool():
    captured = await _run(allow_ask=False)
    assert not any("ask_user_to_pick" in t for t in captured["tools"])
    assert not any("ask_user_to_pick" in t for t in captured["options"].allowed_tools)


async def test_reply_capable_callers_keep_the_ask_tool():
    """SMS keys pending options by phone number, legacy /chat by session_id —
    both can actually resolve a pick, so neither loses the capability."""
    captured = await _run(allow_ask=True)
    assert any("ask_user_to_pick" in t for t in captured["tools"])
    assert any("ask_user_to_pick" in t for t in captured["options"].allowed_tools)


async def test_no_ask_prompt_tells_the_model_to_resolve_by_ranking():
    captured = await _run(allow_ask=False)
    prompt = " ".join(captured["options"].system_prompt.split())
    assert "no reply path" in prompt
    # It must tell the model to decide for itself rather than ask.
    assert "commit to the best candidate" in prompt


async def test_ask_capable_prompt_is_unchanged():
    captured = await _run(allow_ask=True)
    assert captured["options"].system_prompt == agent_module.SYSTEM_PROMPT


def test_allow_ask_defaults_to_true():
    """A new caller must not silently lose disambiguation — opting out is
    explicit."""
    import inspect

    sig = inspect.signature(agent_module.run_agent)
    assert sig.parameters["allow_ask"].default is True


def test_outcome_carries_the_chosen_edition():
    outcome = agent_module.AgentOutcome(
        kind="committed", title="Dune", author="Frank Herbert",
        narrator="Scott Brick", file_format="M4B 64kbps",
    )
    assert outcome.narrator == "Scott Brick"
    assert outcome.file_format == "M4B 64kbps"


def test_outcome_edition_defaults_to_empty_not_none():
    """Rendered directly on the card — None would print as "None"."""
    outcome = agent_module.AgentOutcome(kind="committed")
    assert outcome.narrator == ""
    assert outcome.file_format == ""


async def test_agent_crash_message_is_human_not_a_python_exception():
    """A dead claude CLI (expired subscription token, most often) raised out of
    query() and the raw "Exception: Command failed with exit code 1" landed on
    the card. Verified live on basil, where the token had been expired a month."""
    async def boom(prompt, options):
        raise RuntimeError("Command failed with exit code 1")
        yield

    with (
        patch.object(agent_module, "create_sdk_mcp_server", lambda name, tools: object()),
        patch.object(agent_module, "query", boom),
    ):
        outcome = await agent_module.run_agent(
            "dune", "s1", object(), AsyncMock(), allow_ask=False
        )

    assert outcome.kind == "error"
    assert "Exception" not in outcome.message
    assert "exit code" not in outcome.message
    assert outcome.message.endswith(".")


# --- prompt capability guards -----------------------------------------------
#
# These assert the prompt still instructs the behaviours we care about. They
# can't prove the model complies (that's prompt compliance — non-deterministic),
# but they stop a future edit quietly dropping a rule we added for a reason.


def test_prompt_prefers_the_single_book_over_a_collection():
    """Live run: asked for Project Hail Mary, got a 2.8GB three-book box set."""
    p = agent_module.SYSTEM_PROMPT.lower()
    assert "collection" in p and "omnibus" in p
    assert "standalone" in p


def test_prompt_handles_requests_that_are_not_exact_titles():
    p = agent_module.SYSTEM_PROMPT.lower()
    for capability in ("series position", "author only", "typos"):
        assert capability.split()[0] in p, f"prompt lost: {capability}"
    # Descriptive and vibe-based requests.
    assert "surprise me" in p
    assert "vague" in p


def test_prompt_asks_for_narrator_and_format_on_commit():
    """They're rendered on the card — that's how a wrong pick gets noticed."""
    assert "narrator, format" in agent_module.SYSTEM_PROMPT or (
        "narrator" in agent_module.SYSTEM_PROMPT
        and "format" in agent_module.SYSTEM_PROMPT
    )


def test_prompt_forbids_inventing_runtime_or_eta():
    assert "Never invent" in agent_module.SYSTEM_PROMPT


def test_no_ask_clause_forbids_ending_on_a_question():
    # Normalise: the clause is hard-wrapped, so the phrase spans a newline.
    clause = " ".join(agent_module.NO_ASK_CLAUSE.split())
    assert "Never end your turn with a question" in clause


# --- narration during the agent phase ---------------------------------------
#
# Measured on a real run: 67 seconds between "Searching for X" and the agent's
# announce, with nothing in between. That's the longest gap in the whole
# lifecycle and it lands when the user is most attentive.


class _RecordingBus:
    """Chat-shaped sink: has emit(), like StreamEventBus."""

    def __init__(self):
        self.events = []

    def emit(self, event, data):
        self.events.append((event, data))

    def send(self, _to, text):
        self.events.append(("send", {"text": text}))


class _SmsOnlySink:
    """SMS-shaped sink: send only, no emit."""

    def __init__(self):
        self.sent = []

    def send(self, _to, text):
        self.sent.append(text)


def test_narrate_emits_a_progress_frame_on_a_chat_sink():
    bus = _RecordingBus()
    agent_module._narrate(bus, "searching", "Looking for x…")
    assert bus.events == [("progress", {"stage": "searching", "text": "Looking for x…"})]


def test_narrate_is_a_noop_for_the_sms_sink():
    """The SMS client has no emit(); pushing a dict at it would be a crash or
    a nonsense text message."""
    sink = _SmsOnlySink()
    agent_module._narrate(sink, "searching", "Looking for x…")
    assert sink.sent == []


async def test_search_narrates_before_doing_the_slow_work():
    """The search tool is the single longest step in the agent phase."""
    captured = {}

    def fake_server(name, tools):
        captured["tools"] = {getattr(t, "name", None) or t.__name__: t for t in tools}
        return object()

    async def fake_query(prompt, options):
        return
        yield

    bus = _RecordingBus()
    with (
        patch.object(agent_module, "create_sdk_mcp_server", fake_server),
        patch.object(agent_module, "query", fake_query),
        patch.object(agent_module, "_search_pipeline_sync",
                     lambda q, limit: {"book": {}, "results": []}),
    ):
        await agent_module.run_agent("dune", "s1", object(), bus, allow_ask=False)
        tool = captured["tools"]["search_audiobookbay"]
        handler = getattr(tool, "handler", None) or tool
        await handler({"query": "dune", "limit": 5})

    stages = [d.get("stage") for e, d in bus.events if e == "progress"]
    assert "searching" in stages
