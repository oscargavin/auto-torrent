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
from pathlib import Path

from typing import Awaitable, Callable

from ..cli import _execute_download_bg, _read_state, _resolve_status
from ..config import STATE_DIR
from .audiobookshelf import ABSClient
from .settings import Settings
from .sms import SMSClient

logger = logging.getLogger("atb.worker")

POLL_INTERVAL_S = 15
POLL_TIMEOUT_S = 60 * 60       # 60 min total per attempt
STALL_GRACE_S = 3 * 60         # progress must move within 3 min or we declare stalled


def _sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


def _organize_files(download_path: str, library_path: str, author: str, title: str) -> Path:
    """Move downloaded files into ABS library structure: Author/Title/."""
    src = Path(download_path)
    dest = Path(library_path) / _sanitize(author or "Unknown") / _sanitize(title)
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
) -> None:
    """Poll active download to completion, fall back through alternates on stall.

    `display` is the human-friendly title used in messages.
    `author`/`title` drive the ABS library folder.
    `on_download_change` (optional) is invoked with each new download_id when
    the stall handler swaps to a fallback magnet — lets the caller (the jobs
    worker) keep its store pointer current so the cancel handler kills the
    RUNNING subprocess, not the dead original.
    """
    abs_client = ABSClient(settings)
    fallback_announced = False
    attempt = 0

    while True:
        attempt += 1
        download_id = download.get("id")
        logger.info("Polling %s (attempt %d, fallbacks left=%d)", download_id, attempt, len(fallbacks))

        outcome = await _watch_until_done(download_id)
        if outcome == "completed":
            break
        if outcome == "stalled" or outcome == "failed":
            if not fallbacks:
                logger.info("No fallbacks left for %s", display)
                sms.send(phone, f"Couldn't get {display} tonight, sorry — try in the morning?")
                return
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
        return

    final = _refresh_state(download.get("id"))
    if not final:
        sms.send(phone, f"Couldn't read the final state for {display}. The file may still be there.")
        return

    download_path = final.get("path", "")
    try:
        dest = await asyncio.to_thread(
            _organize_files, download_path, settings.abs_library_path, author, title,
        )
        logger.info("Organised %s → %s", display, dest)
    except Exception:
        logger.exception("organise failed")
        sms.send(phone, f"{display} downloaded but I couldn't move it into the library. Try again?")
        return

    try:
        await abs_client.scan_library(settings.abs_library_id)
    except Exception:
        logger.exception("ABS scan failed; files are in place, will be picked up on next scan")

    sms.send(phone, f"✓ {display} is in your library.")


async def _watch_until_done(download_id: str) -> str:
    """Poll one download. Returns 'completed', 'failed', 'stalled', or 'unknown'."""
    elapsed = 0
    last_progress = -1.0
    last_progress_at = time.monotonic()

    while elapsed < POLL_TIMEOUT_S:
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
