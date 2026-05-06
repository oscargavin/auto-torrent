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

from ..cli import _execute_download_bg, _read_state, _resolve_status
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


async def poll_and_finalise(
    download: dict,
    fallbacks: list[dict],
    display: str,
    author: str,
    title: str,
    phone: str,
    settings: Settings,
    sms: SMSClient,
) -> None:
    """Poll active download to completion, fall back through alternates on stall.

    `display` is the human-friendly title used in messages.
    `author`/`title` drive the ABS library folder.
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
                _kill_download(stale)

            next_fb = fallbacks.pop(0)
            bg_title = f"{title} - {author}" if author else title
            try:
                download = await asyncio.to_thread(
                    _execute_download_bg, bg_title, next_fb["magnet"], None,
                )
            except Exception:
                logger.exception("failed to start fallback")
                continue
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
