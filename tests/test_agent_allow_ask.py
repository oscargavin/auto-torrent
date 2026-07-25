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
    prompt = captured["options"].system_prompt
    assert "no reply path" in prompt
    assert "ranking" in prompt


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
