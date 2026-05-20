"""Behavioural tests for the /chat SSE endpoint.

What's covered:
- bearer auth on the new endpoint
- the "no-results" path: agent messages the user, then the loop ends without
  commit/ask. The endpoint must NOT emit an extra `error` event (regression of
  bug B — the user saw a real message followed by a phantom red bubble)
- the "committed" path emits committed → periodic download progress → completed
  (regression of bug A — the chat bubble sat silent for the entire 10–30 min
  download with no percentage)
- a genuine agent crash (no message sent) still surfaces as an error.
"""
from __future__ import annotations

import json
import os
from typing import AsyncIterator
from unittest.mock import patch

import pytest

# Patch env + Twilio client BEFORE the app module loads — Settings() runs at
# import time, and SMSClient is constructed at module scope.
_env = {
    "TWILIO_ACCOUNT_SID": "test",
    "TWILIO_AUTH_TOKEN": "test",
    "TWILIO_PHONE_NUMBER": "+10000000000",
    "ALLOWED_NUMBERS": '["+1234"]',
    "ABS_API_TOKEN": "test",
    "ABS_LIBRARY_ID": "test",
    "ATB_CWD": "/tmp",
    "ATB_API_TOKEN": "test-token",
}

with (
    patch.dict(os.environ, _env),
    patch("auto_torrent.server.sms.Client"),
    patch("auto_torrent.server.sms.RequestValidator"),
):
    from auto_torrent.server import app as app_module
    from auto_torrent.server.agent import AgentOutcome


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _post_chat(
    body: dict, *, token: str | None = "test-token"
) -> tuple[int, list[tuple[str, dict]]]:
    """POST /chat and return (status, events). Events are (name, data) tuples,
    keepalive comments are dropped."""
    from httpx import ASGITransport, AsyncClient

    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with ac.stream("POST", "/chat", headers=headers, json=body) as r:
            if r.status_code != 200:
                return r.status_code, []
            events = await _drain_sse(r.aiter_lines())
            return r.status_code, events


async def _drain_sse(lines: AsyncIterator[str]) -> list[tuple[str, dict]]:
    """Minimal SSE parser: yields (event_name, data_dict) per blank-line frame."""
    out: list[tuple[str, dict]] = []
    event = "message"
    data = ""
    async for line in lines:
        if line == "":
            if data:
                try:
                    out.append((event, json.loads(data)))
                except json.JSONDecodeError:
                    out.append((event, {"_raw": data}))
            event = "message"
            data = ""
            continue
        if line.startswith(":"):
            continue
        if ":" in line:
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event = value
            elif field == "data":
                data = data + "\n" + value if data else value
    return out


# --- auth ---------------------------------------------------------------


@pytest.mark.anyio
async def test_chat_rejects_missing_bearer():
    status, _ = await _post_chat({"query": "x", "session_id": "s"}, token=None)
    assert status == 401


@pytest.mark.anyio
async def test_chat_rejects_wrong_bearer():
    status, _ = await _post_chat(
        {"query": "x", "session_id": "s"}, token="not-the-token"
    )
    assert status == 401


# --- empty query ---------------------------------------------------------


@pytest.mark.anyio
async def test_chat_empty_query_emits_error_event():
    status, events = await _post_chat({"query": "", "session_id": "s"})
    assert status == 200
    assert events == [("error", {"message": "empty query"})]


# --- no-results graceful path (regression: bug B) -----------------------


@pytest.mark.anyio
async def test_no_results_after_agent_message_does_not_emit_error():
    """The agent sent its own "Couldn't find …" message via send_sms (→ bus).
    The /chat handler must NOT then emit an additional error event."""

    async def fake_run_agent(query, phone, settings_, sms_, pending_options=None):
        # Agent's send_sms tool dispatches to the bus we were handed.
        sms_.send(phone, "Couldn't find x. Try the full title or author?")
        return AgentOutcome(
            kind="error",
            message="agent ended without committing or asking",
        )

    with patch.object(app_module, "run_agent", fake_run_agent):
        status, events = await _post_chat({"query": "x", "session_id": "s1"})

    assert status == 200
    names = [name for name, _ in events]
    assert "error" not in names, f"expected no error event, got: {events}"
    # Initial server "Searching…" progress, then the agent's "Couldn't find…"
    # progress. No phantom error after.
    assert names == ["progress", "progress"]
    assert "Searching" in events[0][1]["text"]
    assert events[1][1]["text"].startswith("Couldn't find")


# --- agent crash with no message (positive control for the suppression) ---


@pytest.mark.anyio
async def test_agent_crash_without_message_still_emits_error():
    """If the agent ends in error AND never spoke to the user, the endpoint
    must surface an error so the user sees something."""

    async def fake_run_agent(query, phone, settings_, sms_, pending_options=None):
        return AgentOutcome(kind="error", message="boom")

    with patch.object(app_module, "run_agent", fake_run_agent):
        status, events = await _post_chat({"query": "x", "session_id": "s2"})

    assert status == 200
    names = [name for name, _ in events]
    # An initial "Searching…" progress is emitted by the server itself
    # (system message, doesn't count as agent-messaged), then the genuine error.
    assert "error" in names
    assert events[-1][0] == "error"
    assert "boom" in events[-1][1]["message"]


# --- pre-commit gap: instant feedback so the bubble isn't a static "Thinking…" ---


@pytest.mark.anyio
async def test_chat_emits_searching_progress_before_agent_starts():
    """Regression for the user-reported "stuck on Working/Thinking" bug.

    The agent's search phase can take 60–90s before the first send_sms.
    During that time the bubble was a static spinner with no context.
    The server must emit an initial "Searching for '<query>'…" progress
    event before run_agent starts so the bubble has motion immediately."""
    import asyncio

    seen_searching = asyncio.Event()

    async def slow_agent(query, phone, settings_, sms_, pending_options=None):
        # Don't ack until the searching event has been emitted to the bus.
        # (The server should emit it BEFORE calling us.)
        seen_searching.set()
        return AgentOutcome(kind="error", message="never used")

    with patch.object(app_module, "run_agent", slow_agent):
        status, events = await _post_chat(
            {"query": "the eye of the world", "session_id": "s-search"}
        )

    assert status == 200
    progress_texts = [d.get("text", "") for n, d in events if n == "progress"]
    assert progress_texts, "expected an immediate 'Searching…' progress event"
    first = progress_texts[0].lower()
    assert "searching" in first, f"first progress should announce searching: {first!r}"
    assert "eye of the world" in first, (
        f"first progress should echo the query so the user knows it landed: {first!r}"
    )


@pytest.mark.anyio
async def test_initial_searching_does_not_count_as_agent_messaged():
    """The server's own 'Searching…' line must NOT flip bus.messaged — otherwise
    a legit agent crash with no follow-up would be wrongly suppressed."""

    async def crashed_agent(query, phone, settings_, sms_, pending_options=None):
        return AgentOutcome(kind="error", message="agent really crashed")

    with patch.object(app_module, "run_agent", crashed_agent):
        status, events = await _post_chat({"query": "x", "session_id": "s-crash2"})

    assert status == 200
    names = [name for name, _ in events]
    # Initial searching progress is fine, but the error must still surface
    # because the agent itself never spoke.
    assert "progress" in names
    assert "error" in names
    assert names[-1] == "error"


# --- committed → progress pump → completed (regression: bug A) -----------


@pytest.mark.anyio
async def test_committed_path_emits_periodic_progress_then_completed():
    """During a download (committed → completed) the endpoint must emit
    progress events that include a percentage, so the chat bubble shows
    motion instead of an indefinite spinner."""
    import asyncio

    async def fake_run_agent(query, phone, settings_, sms_, pending_options=None):
        return AgentOutcome(
            kind="committed",
            download={"id": "test-id", "progress": 0.0},
            fallbacks=[],
            display="“The Book”",
            title="The Book",
            author="Author",
        )

    # poll_and_finalise blocks long enough that the pump fires a few times.
    async def fake_poll_and_finalise(**kwargs):
        await asyncio.sleep(0.25)

    state_iter = iter([
        {"progress": 0.10, "status": "downloading"},
        {"progress": 0.50, "status": "downloading"},
        {"progress": 0.90, "status": "downloading"},
    ])

    def fake_refresh_state(_id):
        try:
            return next(state_iter)
        except StopIteration:
            return {"progress": 1.0, "status": "completed"}

    with (
        patch.object(app_module, "run_agent", fake_run_agent),
        patch.object(app_module, "poll_and_finalise", fake_poll_and_finalise),
        patch.object(app_module, "_refresh_state", fake_refresh_state, create=True),
        patch.object(app_module, "CHAT_PROGRESS_INTERVAL_S", 0.05, create=True),
    ):
        status, events = await _post_chat({"query": "x", "session_id": "s3"})

    assert status == 200
    names = [name for name, _ in events]

    # First a "Searching…" progress, then committed, then download-progress, then completed.
    assert names[0] == "progress"
    assert "Searching" in events[0][1].get("text", "")
    assert "committed" in names
    assert names[-1] == "completed"
    assert names.index("committed") < names.index("completed")

    download_progress_texts = [
        data.get("text", "")
        for name, data in events
        if name == "progress" and "%" in data.get("text", "")
    ]
    assert download_progress_texts, "expected at least one download-progress event with a %"
    assert download_progress_texts == sorted(
        set(download_progress_texts), key=download_progress_texts.index
    ), f"duplicate percentages emitted: {download_progress_texts}"


# --- ChatEventBus tracks whether the agent ever messaged ----------------


@pytest.mark.anyio
async def test_event_bus_tracks_messages():
    """Unit-level: the bus exposes a `messaged` flag flipped by send()."""
    import asyncio as _asyncio

    async def _make_and_check():
        bus = app_module.ChatEventBus()
        assert bus.messaged is False
        bus.send("ignored", "hello")
        # send() goes via call_soon_threadsafe — flag should flip before the
        # event is enqueued because we set it synchronously on the calling side.
        assert bus.messaged is True

    await _make_and_check()
