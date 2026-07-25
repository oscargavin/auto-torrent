"""Deterministic plumbing: poll BG download, fall back on stall, organise, ABS scan, notify."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from typing import Awaitable, Callable

from ..cli import _execute_download_bg, _read_state, _resolve_status
from ..config import STATE_DIR
from .audiobookshelf import ABSClient
from .event_types import (
    EVENT_PROGRESS,
    STAGE_IMPORT_FAILED,
    STAGE_IMPORTING,
    STAGE_RETRYING,
    STAGE_STALLED,
)
from .settings import Settings
from .sms import SMSClient

logger = logging.getLogger("atb.worker")

POLL_INTERVAL_S = 15
POLL_TIMEOUT_S = 60 * 60       # 60 min per attempt (legacy callers, no job deadline)
JOB_BUDGET_S = 60 * 60         # whole-job wall clock when a deadline is supplied
STALL_GRACE_S = 3 * 60         # progress must move within 3 min or we declare stalled
MAX_REDISCOVERY_ROUNDS = 2     # how many times to re-search when fallbacks run dry


async def _reprobe_seeders(magnet: str) -> int:
    """Live seeder count for a magnet via the DHT probe. 0 on any failure —
    a probe error must not block the stall/fallback decision."""
    try:
        from ..cli import _probe_seeds_batch
        counts = await asyncio.to_thread(_probe_seeds_batch, [magnet])
        return int(counts.get(magnet, 0))
    except Exception:  # noqa: BLE001
        logger.exception("seeder re-probe failed for %s", magnet[:60])
        return 0


async def _rediscover_candidates(query: str, tried_magnets: set[str]) -> list[dict]:
    """Re-run the search pipeline for `query` and return fresh fallback dicts
    ({magnet, title}) whose magnets haven't been tried yet. Empty on any
    failure (ABB down, rate-limited, no query) so rediscovery degrades to
    'give up' rather than crashing the poll."""
    if not query:
        return []
    try:
        from .agent import _search_pipeline_sync
        data = await asyncio.to_thread(_search_pipeline_sync, query, 5)
    except Exception:  # noqa: BLE001
        logger.exception("rediscovery search failed for %r", query)
        return []
    out: list[dict] = []
    for r in data.get("results", []):
        magnet = r.get("magnet")
        if magnet and magnet not in tried_magnets:
            out.append({"magnet": magnet, "title": r.get("title", "")})
    return out


def _emit_event(sink: object, event: str, data: dict) -> None:
    """Emit a structured SSE event through a sink that supports it (the chat /
    jobs bus). The SMS sink only has `send` (string bodies) — for it this is a
    no-op, so the shared poll path never pushes a dict at the SMS channel."""
    emit = getattr(sink, "emit", None)
    if callable(emit):
        emit(event, data)


def _sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


class DownloadNotFinishedError(Exception):
    """The download did not end with a book in the library.

    Every path that abandons a download raises one of these. Before, they
    returned normally and the caller could not tell them apart from success —
    so `_emit_download_and_poll` emitted `completed` and the user was told a
    book was in their library when nothing had been downloaded at all.

    `failure_class` is the machine-readable reason the app renders copy from.
    """

    failure_class = "infra_error"


class ImportIncompleteError(DownloadNotFinishedError):
    """The bytes are down but the book did not make it into the library
    (organise or ABS scan failed). Files remain on disk for a later scan; the
    caller must NOT report success."""

    failure_class = "import_failed"


class NoCandidatesLeftError(DownloadNotFinishedError):
    """Every candidate — the agent's fallbacks and anything rediscovery found —
    has been tried and none of them downloaded."""

    failure_class = "no_seeders"


class DownloadStateLostError(DownloadNotFinishedError):
    """The download reported completion but its state file could not be read
    back, so there is no path to organise from."""


class UnknownDownloadOutcomeError(DownloadNotFinishedError):
    """The poll returned a status we have no branch for. Distinct from the
    others because it means a bug here, not a bad torrent."""


class DownloadTimedOutError(DownloadNotFinishedError):
    """The job's wall-clock budget ran out while the download was still going.

    Distinct from arq killing the job: that surfaces as CancelledError and used
    to reach the user as "worker cancelled", which describes our plumbing
    rather than what happened to their book.
    """

    failure_class = "download_timeout"


@dataclass(frozen=True)
class DownloadResult:
    """What a download+import run actually achieved.

    Replaces the old boolean, which conflated "reached the library" with
    "didn't" and had no room for *why* — leaving the jobs layer nothing to
    classify a failure from.
    """

    ok: bool
    failure_class: str | None = None
    message: str | None = None

    @classmethod
    def success(cls) -> "DownloadResult":
        return cls(ok=True)

    @classmethod
    def from_error(cls, exc: Exception) -> "DownloadResult":
        return cls(
            ok=False,
            failure_class=getattr(exc, "failure_class", "infra_error"),
            message=str(exc) or type(exc).__name__,
        )


def _organize_files(
    download_path: str,
    library_path: str,
    author: str,
    title: str,
    download_id: str | None = None,
) -> Path:
    """Move downloaded files into ABS library structure: Author/Title/.

    Normally lands at the clean ``Author/Title/`` path. If that folder already
    exists and is non-empty — i.e. a *different* download (another job, same
    title) already claimed it — the files go to ``Author/Title [id]/`` instead
    of overwriting, so two concurrent same-title downloads can't corrupt each
    other."""
    src = Path(download_path)
    base = Path(library_path) / _sanitize(author or "Unknown") / _sanitize(title)

    dest = base
    if base.exists() and any(base.iterdir()) and download_id:
        dest = base.parent / f"{base.name} [{_sanitize(download_id)}]"
    dest.mkdir(parents=True, exist_ok=True)

    for item in src.iterdir():
        target = dest / item.name
        if target.exists():
            target.unlink() if target.is_file() else shutil.rmtree(target)
        shutil.move(str(item), str(dest))

    if src.exists() and not any(src.iterdir()):
        src.rmdir()

    return dest


def _refresh_state(download_id: str) -> dict | None:
    state = _read_state(download_id)
    if not state:
        return None
    state["status"] = _resolve_status(state)
    return state


def _kill_download(state: dict) -> None:
    pid = state.get("pid")
    if not pid:
        return
    try:
        os.killpg(os.getpgid(pid), 15)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, 15)
        except (OSError, ProcessLookupError):
            pass


async def _kill_download_and_clean(state: dict) -> None:
    """SIGTERM the subprocess group, wait up to 2s for it to actually exit
    (SIGKILL escalation if it ignores us), then remove the partial landing
    directory and the state file.

    Async because the wait-for-death poll must not block the event loop.
    Used by both the cancel handler (jobs/api.py) and any future caller that
    needs a complete, single-call teardown — the previous _kill_download was
    SIGTERM-only and left the caller to do rmtree + state-file unlink in
    parallel idioms that didn't fully converge."""
    _kill_download(state)
    pid = state.get("pid")
    if isinstance(pid, int):
        for _ in range(20):  # up to 2s
            try:
                os.kill(pid, 0)  # signal 0 = "is it alive?"
            except (ProcessLookupError, PermissionError):
                break
            await asyncio.sleep(0.1)
        else:
            # Still alive after 2s — SIGKILL the group.
            try:
                os.killpg(os.getpgid(pid), 9)
            except (OSError, ProcessLookupError):
                pass
    landing_path = state.get("path")
    if landing_path:
        try:
            shutil.rmtree(landing_path, ignore_errors=True)
        except Exception:  # noqa: BLE001
            logger.exception("kill_and_clean: rmtree %s failed", landing_path)
    download_id = state.get("id")
    if download_id:
        try:
            (STATE_DIR / f"{download_id}.json").unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            logger.exception("kill_and_clean: unlink state %s failed", download_id)


async def poll_and_finalise(
    download: dict,
    fallbacks: list[dict],
    display: str,
    author: str,
    title: str,
    phone: str,
    settings: Settings,
    sms: SMSClient,
    on_download_change: Callable[[str], Awaitable[None]] | None = None,
    query: str | None = None,
    deadline: float | None = None,
) -> None:
    """Poll active download to completion, fall back through alternates on stall.

    `display` is the human-friendly title used in messages.
    `author`/`title` drive the ABS library folder.
    `query` (optional) is the original search text — when the fixed fallback
    list runs dry on a stall, it's re-run through the search pipeline to
    discover fresh candidates rather than giving up.
    `on_download_change` (optional) is invoked with each new download_id when
    the stall handler swaps to a fallback magnet — lets the caller (the jobs
    worker) keep its store pointer current so the cancel handler kills the
    RUNNING subprocess, not the dead original.

    `deadline` (optional, a time.monotonic() value) caps the WHOLE job rather
    than each attempt. Without it, POLL_TIMEOUT_S applies per attempt and
    resets on every fallback swap and grace extension — so the real worst case
    is unbounded, and arq's own 1h job timeout would kill the job mid-attempt
    and report it as "worker cancelled". Passing a deadline makes every
    attempt, swap and extension draw from one budget. Left None for the SMS
    and legacy /chat callers, which keep today's behaviour.
    """
    abs_client = ABSClient(settings)
    fallback_announced = False
    attempt = 0
    rediscovery_rounds = 0
    # Magnets we've already tried, so rediscovery never re-suggests a dead one.
    tried_magnets: set[str] = set()
    if download.get("magnet"):
        tried_magnets.add(download["magnet"])
    # Magnets we've already given a second chance to on a seeder re-probe — a
    # magnet only earns one grace extension before we move on.
    grace_extended: set[str] = set()

    while True:
        attempt += 1
        download_id = download.get("id")
        logger.info("Polling %s (attempt %d, fallbacks left=%d)", download_id, attempt, len(fallbacks))

        outcome = await _watch_until_done(download_id, deadline=deadline)
        if outcome == "completed":
            break
        if outcome == "timed_out":
            logger.info("Job budget exhausted for %s", display)
            stale = _refresh_state(download_id)
            if stale:
                await _kill_download_and_clean(stale)
            sms.send(phone, f"{display} was taking too long, so I stopped it.")
            raise DownloadTimedOutError(display)
        if outcome == "stalled" or outcome == "failed":
            # R14: on a stall (not a hard failure), check whether the torrent
            # still has seeders. If it does it's just slow — give it one more
            # grace window rather than throwing away a viable download.
            if outcome == "stalled":
                cur_state = _refresh_state(download_id) or {}
                cur_magnet = cur_state.get("magnet")
                # Say so as soon as the bytes stop, not when we eventually give
                # up and swap magnets — that can be tens of minutes later, and
                # until then a frozen download renders exactly like a slow one.
                _emit_event(sms, EVENT_PROGRESS, {
                    "stage": STAGE_STALLED,
                    "percent": int(float(cur_state.get("progress") or 0) * 100),
                    "peers": cur_state.get("peers"),
                    "text": f"{display} has stopped moving — checking for other sources…",
                })
                if cur_magnet and cur_magnet not in grace_extended:
                    if await _reprobe_seeders(cur_magnet) > 0:
                        grace_extended.add(cur_magnet)
                        logger.info("Stall but %s still seeded — extending grace", display)
                        _emit_event(sms, EVENT_PROGRESS, {
                            "stage": STAGE_RETRYING,
                            "text": f"{display} is slow but still seeded — giving it longer…",
                        })
                        continue

            if not fallbacks:
                # R13: the fixed list is exhausted — re-search for fresh
                # candidates before giving up, capped to bound runtime.
                if rediscovery_rounds < MAX_REDISCOVERY_ROUNDS and query:
                    rediscovery_rounds += 1
                    logger.info("Rediscovery round %d for %s", rediscovery_rounds, display)
                    found = await _rediscover_candidates(query, tried_magnets)
                    if found:
                        fallbacks.extend(found)
                if not fallbacks:
                    logger.info("No fallbacks left for %s", display)
                    sms.send(phone, f"Couldn't get {display} tonight, sorry — try in the morning?")
                    raise NoCandidatesLeftError(display)
            if not fallback_announced:
                sms.send(phone, f"That one stalled — trying another version…")
                fallback_announced = True

            stale = _refresh_state(download_id)
            if stale:
                # Unified kill+wait+SIGKILL+rmtree+unlink — the fallback writes
                # to the same DOWNLOAD_DIR/<sanitize(title)> path, so anything
                # still on disk would get merged with the fallback at organise
                # time and produce a malformed ABS item.
                await _kill_download_and_clean(stale)

            next_fb = fallbacks.pop(0)
            tried_magnets.add(next_fb["magnet"])
            _emit_event(sms, EVENT_PROGRESS, {
                "stage": STAGE_RETRYING,
                "text": f"Trying another copy of {display}…",
            })
            bg_title = f"{title} - {author}" if author else title
            try:
                download = await asyncio.to_thread(
                    _execute_download_bg, bg_title, next_fb["magnet"], None,
                )
            except Exception:
                logger.exception("failed to start fallback")
                continue
            # Tell the caller about the new subprocess so cancel can find it.
            new_id = download.get("id")
            if on_download_change and new_id:
                try:
                    await on_download_change(new_id)
                except Exception:  # noqa: BLE001
                    logger.exception("on_download_change(%s) raised", new_id)
            await asyncio.sleep(0)
            continue
        # Unknown outcome → bail.
        sms.send(phone, f"Something odd happened with {display}. Try again?")
        raise UnknownDownloadOutcomeError(display)

    # The bytes are down; the user-visible work now is organise + ABS scan.
    # Surface that as a distinct stage so the chat/jobs UI shows "importing"
    # rather than sitting at 100% "downloading". No-op for the SMS sink.
    _emit_event(sms, EVENT_PROGRESS, {"stage": STAGE_IMPORTING, "percent": 100,
                                      "text": f"Adding {display} to your library…"})

    final = _refresh_state(download.get("id"))
    if not final:
        sms.send(phone, f"Couldn't read the final state for {display}. The file may still be there.")
        raise DownloadStateLostError(display)

    download_path = final.get("path", "")
    try:
        dest = await asyncio.to_thread(
            _organize_files, download_path, settings.abs_library_path,
            author, title, download.get("id"),
        )
        logger.info("Organised %s → %s", display, dest)
    except Exception as e:
        logger.exception("organise failed")
        sms.send(phone, f"{display} downloaded but I couldn't move it into the library. Try again?")
        _emit_event(sms, EVENT_PROGRESS, {"stage": STAGE_IMPORT_FAILED, "percent": 100,
                                          "text": f"{display} downloaded but couldn't be imported."})
        raise ImportIncompleteError(display) from e

    try:
        await abs_client.scan_library(settings.abs_library_id)
    except Exception as e:
        # Files are on disk; a later scan will pick them up. But we must NOT
        # tell the user it's in the library — surface "downloaded, not yet
        # imported" instead and signal the caller to skip the success event.
        logger.exception("ABS scan failed; files are in place, will be picked up on next scan")
        sms.send(phone, f"{display} downloaded — it'll appear after the next library scan.")
        _emit_event(sms, EVENT_PROGRESS, {"stage": STAGE_IMPORT_FAILED, "percent": 100,
                                          "text": f"{display} downloaded, not yet imported."})
        raise ImportIncompleteError(display) from e

    sms.send(phone, f"✓ {display} is in your library.")


async def _watch_until_done(download_id: str, *, deadline: float | None = None) -> str:
    """Poll one download.

    Returns 'completed', 'failed', 'stalled', 'timed_out', or 'unknown'.

    With a `deadline` the job-level budget governs and 'timed_out' is
    terminal for the whole job. Without one, POLL_TIMEOUT_S applies per
    attempt — the legacy behaviour, where `elapsed` resets on every call.
    """
    elapsed = 0
    last_progress = -1.0
    last_progress_at = time.monotonic()

    while deadline is not None or elapsed < POLL_TIMEOUT_S:
        if deadline is not None and time.monotonic() >= deadline:
            return "timed_out"

        await asyncio.sleep(POLL_INTERVAL_S)
        elapsed += POLL_INTERVAL_S

        state = _refresh_state(download_id)
        if not state:
            return "unknown"

        status = state.get("status", "unknown")
        progress = float(state.get("progress", 0) or 0)
        logger.info("Download %s: %s (%.0f%%)", download_id, status, progress * 100)

        if status == "completed":
            return "completed"
        if status == "failed":
            return "failed"

        if progress > last_progress + 1e-9:
            last_progress = progress
            last_progress_at = time.monotonic()
        elif time.monotonic() - last_progress_at >= STALL_GRACE_S:
            return "stalled"

    return "stalled"


async def get_active_downloads(settings: Settings) -> list[dict]:
    """For the SMS 'status' command. Returns active state dicts."""
    result = await asyncio.to_thread(
        _run_atb_status, settings.atb_cwd,
    )
    if not result:
        return []
    downloads = result.get("downloads", [])
    return [d for d in downloads if d.get("status") == "downloading"]


def _run_atb_status(cwd: str) -> dict | None:
    uv = "/home/oscar/.local/bin/uv"
    cmd = [uv, "run", "atb", "status", "--json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=cwd)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
