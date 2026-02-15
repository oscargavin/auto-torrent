"""Background pipeline: search → download → organize → scan → notify."""

import asyncio
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from .audiobookshelf import ABSClient
from .settings import Settings
from .sms import SMSClient

logger = logging.getLogger("atb.worker")

POLL_INTERVAL = 15
POLL_TIMEOUT = 3600  # 60 minutes
SEARCH_TIMEOUT = 300  # 5 minutes


def _run_atb(args: list[str], cwd: str, timeout: int = SEARCH_TIMEOUT) -> dict | None:
    """Run an atb CLI command and parse JSON output."""
    uv = "/home/oscar/.local/bin/uv"
    cmd = [uv, "run", "atb", *args, "--json"]
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
    )
    if result.returncode != 0:
        logger.error("atb failed (exit %d): %s", result.returncode, result.stderr)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.error("Failed to parse atb output: %s", result.stdout[:500])
        return None


def _sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


def _organize_files(download_path: str, library_path: str, author: str, title: str) -> Path:
    """Move downloaded files to ABS library structure: Author/Title/."""
    src = Path(download_path)
    dest = Path(library_path) / _sanitize(author or "Unknown") / _sanitize(title)
    dest.mkdir(parents=True, exist_ok=True)

    for item in src.iterdir():
        target = dest / item.name
        if target.exists():
            target.unlink() if target.is_file() else shutil.rmtree(target)
        shutil.move(str(item), str(dest))

    # Clean up empty source directory
    if src.exists() and not any(src.iterdir()):
        src.rmdir()

    return dest


async def process_audiobook_request(
    query: str,
    phone: str,
    settings: Settings,
    sms: SMSClient,
) -> None:
    """Full pipeline: search → download → organize → scan → notify."""
    abs_client = ABSClient(settings)

    # Step 1: Search
    logger.info("Searching for: %s", query)
    search_result = await asyncio.to_thread(
        _run_atb,
        ["search", query, "--auto", "--bg", "--limit", "3"],
        settings.atb_cwd,
    )

    if not search_result:
        sms.send(phone, f'Couldn\'t find "{query}". Try a different title?')
        return

    if "error" in search_result:
        logger.error("Search error: %s", search_result["error"])
        sms.send(phone, f'Couldn\'t find "{query}". Try a different title?')
        return

    # Extract info from search result
    download = search_result.get("download")
    book = search_result.get("book", {})
    title = book.get("title") or query
    author = book.get("author") or ""

    if not download or "id" not in download:
        sms.send(phone, f'Couldn\'t find "{query}". Try a different title?')
        return

    download_id = download["id"]
    download_path = download.get("path", "")

    # Step 2: Notify — found and downloading
    display = f'"{title}"'
    if author:
        display += f" by {author}"
    sms.send(phone, f"Found {display}! Downloading now...")

    # Step 3: Poll for completion
    logger.info("Polling download %s", download_id)
    elapsed = 0
    final_status = "failed"

    while elapsed < POLL_TIMEOUT:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

        status_result = await asyncio.to_thread(
            _run_atb, ["status", download_id], settings.atb_cwd, timeout=30,
        )

        if not status_result:
            continue

        status = status_result.get("status", "unknown")
        progress = status_result.get("progress", 0)
        logger.info("Download %s: %s (%.0f%%)", download_id, status, progress * 100)

        if status == "completed":
            final_status = "completed"
            break
        elif status == "failed":
            final_status = "failed"
            break

    if final_status != "completed":
        sms.send(phone, "Download didn't work. Send the title again to retry?")
        return

    # Step 4: Organize files into ABS library
    try:
        dest = await asyncio.to_thread(
            _organize_files, download_path, settings.abs_library_path, author, title,
        )
        logger.info("Organized files to: %s", dest)
    except Exception:
        logger.exception("Failed to organize files")
        sms.send(phone, "Something went wrong. Try again in a few minutes?")
        return

    # Step 5: Trigger ABS library scan
    try:
        await abs_client.scan_library(settings.abs_library_id)
        logger.info("Triggered ABS library scan")
    except Exception:
        logger.exception("ABS scan failed (files are in place, will be picked up on next scan)")

    # Step 6: Notify — ready to listen
    sms.send(phone, f'"{title}" is ready! Open the app to listen.')


async def get_active_downloads(settings: Settings) -> list[dict]:
    """Get all active downloads for status reporting."""
    result = await asyncio.to_thread(
        _run_atb, ["status"], settings.atb_cwd, timeout=30,
    )
    if not result:
        return []
    downloads = result.get("downloads", [])
    return [d for d in downloads if d.get("status") == "downloading"]
