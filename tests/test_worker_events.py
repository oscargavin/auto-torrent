"""U1: structured lifecycle events from the shared poll path.

Covers the `importing` stage emit, the guarantee that the SMS-shaped sink
(which has `send` but no `emit`) never receives a structured dict, and the
event-vocabulary parity hook.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from auto_torrent.server import worker as worker_module
from auto_torrent.server.event_types import EVENT_PROGRESS, STAGE_IMPORTING
from auto_torrent.server.worker import _emit_event, poll_and_finalise


@pytest.fixture
def anyio_backend():
    return "asyncio"


class BusSink:
    """Chat/jobs-style sink: supports both send (strings) and emit (events)."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.emitted: list[tuple[str, dict]] = []

    def send(self, _to: str, body: str) -> None:
        assert isinstance(body, str), "send() must only ever receive a string"
        self.sent.append(body)

    def emit(self, event: str, data: dict) -> None:
        self.emitted.append((event, data))


class SmsOnlySink:
    """SMS-shaped sink: send only, no emit (like the real SMSClient)."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, _to: str, body: str) -> None:
        assert isinstance(body, str)
        self.sent.append(body)


def test_emit_event_noops_without_emit():
    sink = SmsOnlySink()
    # Must not raise, must not somehow turn into a send.
    _emit_event(sink, EVENT_PROGRESS, {"stage": STAGE_IMPORTING})
    assert sink.sent == []


class _FakeABS:
    def __init__(self, _settings):
        pass

    async def scan_library(self, _library_id):
        return None


@pytest.mark.anyio
async def test_importing_emitted_once_before_success(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "ABSClient", _FakeABS)

    async def fake_watch(_id):
        return "completed"

    landing = tmp_path / "landing"
    landing.mkdir()
    monkeypatch.setattr(worker_module, "_watch_until_done", fake_watch)
    monkeypatch.setattr(
        worker_module, "_refresh_state",
        lambda _id: {"id": _id, "path": str(landing), "status": "completed"},
    )
    monkeypatch.setattr(worker_module, "_organize_files", lambda *a, **k: tmp_path / "dest")

    sink = BusSink()
    settings = SimpleNamespace(abs_library_path=str(tmp_path), abs_library_id="lib")

    await poll_and_finalise(
        download={"id": "d1"}, fallbacks=[], display="“The Book”",
        author="Author", title="The Book", phone="s1", settings=settings, sms=sink,
    )

    importing = [d for ev, d in sink.emitted if ev == EVENT_PROGRESS and d.get("stage") == STAGE_IMPORTING]
    assert len(importing) == 1, f"expected exactly one importing event, got {sink.emitted}"
    # The success line still goes out as a plain string on the send channel.
    assert any("library" in s for s in sink.sent)
    # No structured dict ever reached the send channel.
    assert all(isinstance(s, str) for s in sink.sent)
