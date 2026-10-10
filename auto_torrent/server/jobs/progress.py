"""The download half of a job: `committed`, a progress pump, then the poll.

Moved out of app.py so the arq worker does not import the web app to run a
download.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..event_types import EVENT_PROGRESS, STAGE_DOWNLOADING, STAGE_FOUND
from ..settings import Settings
from ..worker import (
    DownloadNotFinishedError,
    DownloadResult,
    _refresh_state,
    poll_and_finalise,
)

if TYPE_CHECKING:
    from ..agent import AgentOutcome

logger = logging.getLogger("atb.jobs.progress")
settings = Settings()

# How often the chat path polls download state and emits "Downloading… N%" as
# a progress event so the bubble shows motion. Tests override.
# 10s, down from 30s: the pump emits only on percent change, so a slow
# download is unaffected while a fast one stops leaping ~40 points a frame.
CHAT_PROGRESS_INTERVAL_S: float = 10.0

# A frame at least this often while a download is live, even when the
# whole-number percentage has not moved. On a slow swarm one percent can take
# minutes, and a card that hears nothing for that long cannot tell a slow
# download from a dead one — nor can its ETA, which is refreshed only by a frame.
CHAT_PROGRESS_HEARTBEAT_S: float = 30.0
# How long the rate may sit at zero before the card says so. Well under the
# poll layer's STALL_GRACE_S (3 min): that is when the server gives up on a
# copy, not when the user should first hear that nothing is arriving.
CHAT_IDLE_NOTICE_S: float = 30.0


async def _chat_progress_pump(
    current: dict, bus: Any, stop: asyncio.Event
) -> None:
    """Emit a progress event whenever what the card should say changes.

    Why: poll_and_finalise blocks for the entire download (minutes to hours)
    and says nothing while the bytes move. The pump fills the gap so the card
    shows motion.

    `current` is a live `{"id", "copy"}` holder rather than a fixed id: a stall
    swaps to a fallback copy under a new download id, and the old state file is
    unlinked. A pump bound to the first id read nothing for the rest of the job,
    so a fallback that downloaded to 100% did so with no progress on the card.
    """
    last_key: tuple | None = None
    last_emit = 0.0
    last_id: str | None = None
    zero_since: float | None = None
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=CHAT_PROGRESS_INTERVAL_S)
            return
        except asyncio.TimeoutError:
            pass
        download_id = current["id"]
        copy = current["copy"]
        if download_id != last_id:
            # A new copy starts from zero; nothing about the old one carries.
            last_id, last_key, zero_since = download_id, None, None
        state = _refresh_state(download_id)
        if not state:
            continue
        progress = float(state.get("progress") or 0)
        pct = int(progress * 100)
        speed = int(state.get("speed_bytes_per_s") or 0)
        eta = state.get("eta_s")
        now = time.monotonic()

        if pct >= 100 and last_key is not None and last_key[1] >= 100:
            # The bytes are down and the card has been told. From here the
            # import owns the card; a heartbeat re-sending "downloading 100%"
            # would pull it back from "Adding to your library" a step.
            continue

        if pct == 0 and speed <= 0 and eta is None:
            # Still fetching torrent metadata, or no peer has sent a byte yet.
            # A determinate bar pinned at 0% reads as frozen for up to two
            # minutes here, so this stays on the "found" step with honest words.
            # A fallback copy is covered by the `retrying` frame that started it.
            if copy > 1:
                continue
            frame = {
                "stage": STAGE_FOUND,
                "percent": 0,
                "peers": state.get("peers"),
                "text": "Connecting to people sharing it…",
            }
        else:
            # A finished download has a zero rate too; that is not waiting.
            zero_since = (zero_since or now) if speed <= 0 and pct < 100 else None
            waiting = zero_since is not None and now - zero_since >= CHAT_IDLE_NOTICE_S
            if waiting:
                text = f"Waiting for data… {pct}%"
            elif copy > 1:
                text = f"Downloading another copy… {pct}%"
            else:
                text = f"Downloading… {pct}%"
            frame = {
                "percent": pct,
                # Exact progress, for a slow swarm: there one whole percent
                # takes minutes, and the heartbeat's fresh fraction is the only
                # thing that visibly moves.
                "fraction": round(progress, 4),
                "speed_bytes_per_s": speed,
                "peers": state.get("peers"),
                # No ETA while nothing arrives: the last one assumed a rate
                # that has gone, and the client extrapolates the bar from it.
                "eta_s": None if waiting else eta,
                "stage": STAGE_DOWNLOADING,
                "text": text,
            }
            if copy > 1:
                frame["copy"] = copy

        key = (frame["stage"], pct, frame["text"])
        # `text` is retained for back-compat (old consumers + the SSE test's
        # %-check). Change-or-heartbeat keeps a multi-hour download to a few
        # hundred events, clear of the Redis stream MAXLEN trim.
        if key != last_key or now - last_emit >= CHAT_PROGRESS_HEARTBEAT_S:
            bus.emit(EVENT_PROGRESS, frame)
            last_key, last_emit = key, now


async def _emit_download_and_poll(
    bus: Any,
    outcome: AgentOutcome,
    *,
    on_download_change: Callable[[str], Awaitable[None]] | None = None,
    query: str | None = None,
    deadline: float | None = None,
) -> DownloadResult:
    """Emit `committed`, then run the poll with a progress pump.

    Returns a DownloadResult. `ok` is true only when the book actually reached
    the library; otherwise `failure_class` says why, which is what the jobs
    layer renders user-facing copy from. This used to be a bool, and every
    path that abandoned a download returned normally — so `completed` fired
    and the user was told a book they never received was in their library.

    This function never publishes a terminal `completed` or `error` frame.
    JobStore.update_status is the only producer of terminal events. A second
    frame from here would race it, because bus.emit is fire-and-forget through
    call_soon_threadsafe. The SSE stream closes on whichever frame lands first,
    and that could be the one without the job's error detail.

    `on_download_change` forwards the per-fallback download_id update from
    poll_and_finalise to the caller — the jobs worker uses it to keep
    store.set_download_id current so cancel kills the right subprocess."""
    download = outcome.download or {}
    download_id = download.get("id", "")
    # The edition rides on `committed` so the card shows which copy this is the
    # moment it is chosen, not on the next 30s snapshot poll.
    bus.emit(
        "committed",
        {
            "id": download_id, "title": outcome.title, "author": outcome.author,
            "display": outcome.display, "narrator": outcome.narrator or "",
            "format": outcome.file_format or "",
        },
    )
    # `committed` carries the book; this carries the lifecycle position, so the
    # card can move off "searching" before the first byte-progress frame lands
    # (which only fires on a percent change, and can be a while on a slow start).
    bus.emit(EVENT_PROGRESS, {"stage": STAGE_FOUND, "percent": 0,
                              "text": "Found it — starting the download…"})
    stop = asyncio.Event()
    current = {"id": download_id, "copy": 1}

    async def _on_download_change(new_id: str) -> None:
        current["id"] = new_id
        current["copy"] += 1
        if on_download_change is not None:
            await on_download_change(new_id)

    pump = asyncio.create_task(_chat_progress_pump(current, bus, stop))
    try:
        await poll_and_finalise(
            download=download,
            fallbacks=outcome.fallbacks,
            display=outcome.display,
            author=outcome.author,
            title=outcome.title,
            settings=settings,
            sink=bus,
            on_download_change=_on_download_change,
            query=query,
            deadline=deadline,
        )
        return DownloadResult.success()
    except DownloadNotFinishedError as e:
        # An abandoned or half-finished download. poll_and_finalise has already
        # messaged the user and emitted the relevant stage; we only translate
        # the reason for the caller. Never `completed`.
        logger.info("download did not finish for %s: %s", outcome.display, type(e).__name__)
        return DownloadResult.from_error(e)
    except Exception as e:  # noqa: BLE001
        logger.exception("poll_and_finalise crashed")
        return DownloadResult.from_error(e)
    finally:
        stop.set()
        try:
            await pump
        except Exception:  # noqa: BLE001
            pass

