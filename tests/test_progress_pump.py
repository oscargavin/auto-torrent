"""The chat/jobs progress pump: what the card hears while bytes move."""

import asyncio
from unittest.mock import patch

import pytest

from auto_torrent.server import app as app_module


class _Bus:
    def __init__(self) -> None:
        self.frames: list[tuple[str, dict]] = []

    def emit(self, type: str, data: dict | None = None) -> None:
        self.frames.append((type, dict(data or {})))


async def _run_pump(states, *, current=None, ticks=6, heartbeat=3600.0, idle=3600.0):
    """Run the pump for `ticks` wake-ups. `states` maps download id to a list
    of state dicts, one consumed per read; the last one repeats."""
    bus = _Bus()
    current = current or {"id": "dl1", "copy": 1}
    reads = {k: list(v) for k, v in states.items()}

    def refresh(download_id):
        seq = reads.get(download_id)
        if not seq:
            return None
        return seq.pop(0) if len(seq) > 1 else seq[0]

    stop = asyncio.Event()
    with (
        patch.object(app_module, "_refresh_state", refresh),
        patch.object(app_module, "CHAT_PROGRESS_INTERVAL_S", 0.01),
        patch.object(app_module, "CHAT_PROGRESS_HEARTBEAT_S", heartbeat),
        patch.object(app_module, "CHAT_IDLE_NOTICE_S", idle),
    ):
        task = asyncio.create_task(app_module._chat_progress_pump(current, bus, stop))
        for _ in range(ticks):
            await asyncio.sleep(0.012)
            yield_point = current.get("_on_tick")
            if yield_point:
                yield_point()
        stop.set()
        await task
    return bus.frames


def _moving(pct, eta=60, speed=1_000_000):
    return {"progress": pct / 100, "speed_bytes_per_s": speed, "eta_s": eta, "peers": 4}


@pytest.mark.anyio
async def test_metadata_wait_reads_as_connecting_not_a_frozen_zero():
    frames = await _run_pump({"dl1": [{"progress": 0.0, "peers": 0}]}, ticks=3)
    assert frames, "expected a frame while the torrent is still connecting"
    stage, data = frames[0][1]["stage"], frames[0][1]
    assert stage == "found"
    assert data["text"] == "Connecting to people sharing it…"
    # One frame, not one per wake-up: nothing changed.
    assert len(frames) == 1


@pytest.mark.anyio
async def test_the_pump_follows_a_fallback_copy():
    """A stall swaps to a new download id and unlinks the old state file. The
    pump used to keep reading the dead id and fell silent for the rest of the job."""
    current = {"id": "dl1", "copy": 1}
    ticks = {"n": 0}

    def swap():
        ticks["n"] += 1
        if ticks["n"] == 2:
            current["id"], current["copy"] = "dl2", 2

    current["_on_tick"] = swap
    frames = await _run_pump(
        {"dl1": [_moving(40)], "dl2": [_moving(5), _moving(12), _moving(30)]},
        current=current,
        ticks=6,
    )
    after = [d for _t, d in frames if d.get("copy") == 2]
    assert after, f"no frames for the fallback copy: {frames}"
    assert [d["percent"] for d in after] == sorted(d["percent"] for d in after)
    assert after[0]["text"].startswith("Downloading another copy…")
    assert all(d["stage"] == "downloading" for d in after)


@pytest.mark.anyio
async def test_a_fallback_copy_connecting_stays_on_the_retrying_frame():
    """The retrying frame already says what is happening; a `found` frame here
    would move the card's checklist backwards."""
    frames = await _run_pump(
        {"dl2": [{"progress": 0.0}]}, current={"id": "dl2", "copy": 2}, ticks=3
    )
    assert frames == []


@pytest.mark.anyio
async def test_heartbeat_resends_an_unchanged_percentage():
    frames = await _run_pump({"dl1": [_moving(1, eta=9000)]}, ticks=4, heartbeat=0.0)
    assert len(frames) >= 3, "a slow download must still be heard from"
    assert {d["percent"] for _t, d in frames} == {1}
    assert frames[0][1]["fraction"] == 0.01


@pytest.mark.anyio
async def test_zero_rate_says_waiting_and_drops_the_stale_eta():
    frames = await _run_pump(
        {"dl1": [_moving(40), {"progress": 0.40, "speed_bytes_per_s": 0, "eta_s": None}]},
        ticks=4,
        idle=0.0,
    )
    last = frames[-1][1]
    assert last["text"] == "Waiting for data… 40%"
    assert last["eta_s"] is None
    assert last["stage"] == "downloading"


@pytest.mark.anyio
async def test_committed_carries_the_edition():
    bus = _Bus()

    async def fake_poll(**_kw):
        return None

    with (
        patch.object(app_module, "poll_and_finalise", fake_poll),
        patch.object(app_module, "_refresh_state", lambda _id: None),
    ):
        await app_module._emit_download_and_poll(
            bus, download={"id": "dl1"}, fallbacks=[], display="“Dune”",
            title="Dune", author="Frank Herbert", session="s",
            narrator="Scott Brick", file_format="M4B",
        )
    committed = [d for t, d in bus.frames if t == "committed"]
    assert committed[0]["narrator"] == "Scott Brick"
    assert committed[0]["format"] == "M4B"


@pytest.mark.anyio
async def test_a_swap_is_still_reported_to_the_caller():
    """The pump's holder wraps on_download_change; the jobs worker's own
    callback (which keeps cancel pointed at the live copy) must still fire."""
    seen: list[str] = []

    async def track(new_id: str) -> None:
        seen.append(new_id)

    async def fake_poll(**kw):
        await kw["on_download_change"]("dl2")

    with (
        patch.object(app_module, "poll_and_finalise", fake_poll),
        patch.object(app_module, "_refresh_state", lambda _id: None),
    ):
        await app_module._emit_download_and_poll(
            _Bus(), download={"id": "dl1"}, fallbacks=[], display="“Dune”",
            title="Dune", author="Frank Herbert", session="s",
            on_download_change=track,
        )
    assert seen == ["dl2"]




@pytest.mark.anyio
async def test_no_heartbeat_once_the_bytes_are_down():
    """Import runs while the pump is still alive. A repeated `downloading 100%`
    there would move the card back from "Adding to your library"."""
    done = {"progress": 1.0, "status": "completed", "speed_bytes_per_s": 0, "eta_s": 0}
    frames = await _run_pump({"dl1": [_moving(97), done]}, ticks=6, heartbeat=0.0, idle=0.0)
    hundred = [d for _t, d in frames if d.get("percent") == 100]
    assert len(hundred) == 1
    assert hundred[0]["text"] == "Downloading… 100%"
