"""Background pipeline: search → download → organize → scan → notify."""

import asyncio
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .audiobookshelf import ABSClient
from .settings import Settings
from .sms import SMSClient

logger = logging.getLogger("atb.worker")

POLL_INTERVAL = 15
POLL_TIMEOUT = 3600  # 60 minutes
SEARCH_TIMEOUT = 300  # 5 minutes


@dataclass(frozen=True)
class SearchDecision:
    auto_download: bool
    chosen_index: int | None
    reason: str


def decide_action(
    results: list[dict], threshold: int = 85, gap: int = 15,
) -> SearchDecision:
    """Decide whether to auto-download or present choices to the user."""
    if not results:
        return SearchDecision(auto_download=False, chosen_index=None, reason="no results")

    top_score = results[0].get("score", 0)
    second_score = results[1].get("score", 0) if len(results) > 1 else 0

    # Single result: lower threshold (75)
    if len(results) == 1:
        if top_score >= (threshold - 10):
            return SearchDecision(auto_download=True, chosen_index=0, reason="single clear match")
        return SearchDecision(auto_download=False, chosen_index=None, reason="single result, low confidence")

    # Multiple results: need high score AND sufficient gap
    if top_score >= threshold and (top_score - second_score) >= gap:
        return SearchDecision(auto_download=True, chosen_index=0, reason="clear winner")

    return SearchDecision(auto_download=False, chosen_index=None, reason="ambiguous results")


def format_results_sms(results: list[dict], max_results: int = 3) -> str:
    """Format search results as a numbered SMS message."""
    if not results:
        return "I couldn't find any matches. Try a different title?"

    lines = ["I found a few options:"]
    for i, r in enumerate(results[:max_results]):
        title = r.get("title", "Unknown")
        by = r.get("narrator") or r.get("author") or ""
        fmt = r.get("format", "")
        score = r.get("score", 0)

        parts = [title]
        if by:
            parts.append(f"- {by}")
        detail = []
        if fmt:
            detail.append(fmt)
        detail.append(f"{score}%")
        parts.append(f"({', '.join(detail)})")

        lines.append(f"{i + 1}. {' '.join(parts)}")

    lines.append("\nReply with a number to download!")
    return "\n".join(lines)


async def search_audiobook(query: str, settings: Settings) -> dict | None:
    """Run atb search (no auto-download, no background) and return parsed results."""
    result = await asyncio.to_thread(
        _run_atb,
        ["search", query, "--limit", "3"],
        settings.atb_cwd,
    )
    if not result or "error" in result:
        if result:
            logger.error("Search error: %s", result["error"])
        return None
    return result


async def download_audiobook(
    magnet: str, title: str, author: str, settings: Settings,
) -> dict | None:
    """Start a background download for a specific magnet."""
    result = await asyncio.to_thread(
        _run_atb,
        ["download", magnet, "--title", f"{title} - {author}" if author else title, "--bg"],
        settings.atb_cwd,
    )
    return result


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


async def _download_and_notify(
    result: dict,
    phone: str,
    settings: Settings,
    sms: SMSClient,
) -> None:
    """Download a specific result, then organize → scan → notify."""
    title = result.get("title", "Unknown")
    author = result.get("author", "")
    magnet = result.get("magnet", "")

    if not magnet:
        sms.send(phone, "Something went wrong — no download link. Try again?")
        return

    dl_result = await download_audiobook(magnet, title, author, settings)
    if not dl_result:
        sms.send(phone, "Download didn't start. Send the title again to retry?")
        return

    download = dl_result.get("download", {})
    download_id = download.get("id")
    download_path = download.get("path", "")

    if not download_id:
        sms.send(phone, "Download didn't start. Send the title again to retry?")
        return

    display = f'"{title}"'
    if author:
        display += f" by {author}"

    # Poll for completion
    logger.info("Polling download %s", download_id)
    elapsed = 0
    final_status = "failed"
    notified_downloading = False

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

        if not notified_downloading:
            sms.send(phone, f"Found {display}! Downloading now...")
            notified_downloading = True

    if final_status != "completed":
        sms.send(phone, "Download didn't work. Send the title again to retry?")
        return

    # Organize files into ABS library
    abs_client = ABSClient(settings)
    try:
        dest = await asyncio.to_thread(
            _organize_files, download_path, settings.abs_library_path, author, title,
        )
        logger.info("Organized files to: %s", dest)
    except Exception:
        logger.exception("Failed to organize files")
        sms.send(phone, "Something went wrong. Try again in a few minutes?")
        return

    # Trigger ABS library scan
    try:
        await abs_client.scan_library(settings.abs_library_id)
        logger.info("Triggered ABS library scan")
    except Exception:
        logger.exception("ABS scan failed (files are in place, will be picked up on next scan)")

    sms.send(phone, f'"{title}" is ready! Open the app to listen.')


async def process_audiobook_request(
    query: str,
    phone: str,
    settings: Settings,
    sms: SMSClient,
) -> None:
    """Search → decide (auto-download vs present choices) → act."""
    from .llm import store_pending_results

    logger.info("Searching for: %s", query)
    search_result = await search_audiobook(query, settings)

    if not search_result:
        sms.send(phone, f'Couldn\'t find "{query}". Try a different title?')
        return

    results = search_result.get("results", [])
    if not results:
        sms.send(phone, f'Couldn\'t find "{query}". Try a different title?')
        return

    decision = decide_action(results)
    logger.info("Decision for '%s': %s (%s)", query, decision.auto_download, decision.reason)

    if decision.auto_download and decision.chosen_index is not None:
        chosen = results[decision.chosen_index]
        await _download_and_notify(chosen, phone, settings, sms)
    else:
        store_pending_results(phone, results)
        sms.send(phone, format_results_sms(results))


async def get_active_downloads(settings: Settings) -> list[dict]:
    """Get all active downloads for status reporting."""
    result = await asyncio.to_thread(
        _run_atb, ["status"], settings.atb_cwd, timeout=30,
    )
    if not result:
        return []
    downloads = result.get("downloads", [])
    return [d for d in downloads if d.get("status") == "downloading"]
