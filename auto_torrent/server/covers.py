"""Cover art for books Audiobookshelf couldn't match on its own.

ABS only derives a cover from what the download actually contains. Plenty of
audiobook releases ship no artwork at all, so the item lands in the library as
a blank tile — 4 of 30 on the live library, including *Steve Jobs*.

The lookup is `audnex.hydrate`, already used by the recommendations path:
Audible search first (square art, the same source ABS's own metadata provider
uses), falling back to OpenLibrary. What this module adds is the retry shapes
that make it work on *library* metadata, which is messier than a user's typed
request — titles carry subtitles and ABS concatenates duplicate author tags.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import requests

from ..audnex import hydrate

logger = logging.getLogger("atb.covers")

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
# " - Subtitle", " : Subtitle" — an audiobook's shelf title, not its real one.
_SUBTITLE = re.compile(r"\s+[-–—:]\s+.*$")
_MIN_COVER_BYTES = 1000


def clean_author(author: str) -> str:
    """Collapse ABS's concatenated author tags.

    A file tagged with the same author in two places surfaces as
    "Anthony Bourdain/Anthony Bourdain", which matches nothing.
    """
    parts = [p.strip() for p in (author or "").split("/") if p.strip()]
    if not parts:
        return ""
    seen: list[str] = []
    for part in parts:
        if part.lower() not in {s.lower() for s in seen}:
            seen.append(part)
    return ", ".join(seen)


def title_variants(title: str) -> list[str]:
    """Progressively simpler titles to try, most specific first.

    "Kitchen Confidential - Adventures in the Culinary Underbelly" finds
    nothing; "Kitchen Confidential" finds it immediately.
    """
    full = (title or "").strip()
    if not full:
        return []
    variants = [full]
    without_subtitle = _SUBTITLE.sub("", full).strip()
    if without_subtitle and without_subtitle != full:
        variants.append(without_subtitle)
    return variants


def find_cover_url(title: str, author: str) -> str | None:
    """First cover found across the title variants, or None."""
    who = clean_author(author)
    for variant in title_variants(title):
        try:
            card = hydrate(variant, who)
        except Exception:  # noqa: BLE001 — never let artwork break a caller
            logger.exception("cover lookup failed for %r", variant)
            continue
        if card and card.cover_url:
            return card.cover_url
    return None


def coverless_items(items: list[dict]) -> list[dict]:
    """Library items ABS has no artwork for."""
    return [i for i in items if not (i.get("media") or {}).get("coverPath")]


def item_title_author(item: dict) -> tuple[str, str]:
    meta = ((item.get("media") or {}).get("metadata")) or {}
    return meta.get("title") or "", meta.get("authorName") or ""


async def backfill_covers(abs_client, library_id: str, *, dry_run: bool = False) -> dict:
    """Find a cover for every item in the library that lacks one.

    Separate from the import path because it fixes what is already there —
    books added before this existed, and anything a future lookup misses and a
    later re-run catches. Safe to run repeatedly: an item that gained a cover
    is simply no longer in the list.
    """
    items = await abs_client.list_items(library_id)
    missing = coverless_items(items)
    fixed: list[str] = []
    unresolved: list[str] = []

    for item in missing:
        title, author = item_title_author(item)
        url = await asyncio.to_thread(find_cover_url, title, author)
        if not url:
            unresolved.append(title)
            continue
        if dry_run:
            fixed.append(title)
            continue
        try:
            await abs_client.set_cover(item["id"], url)
            fixed.append(title)
        except Exception:  # noqa: BLE001
            logger.exception("failed to set cover for %r", title)
            unresolved.append(title)

    return {
        "scanned": len(items),
        "missing": len(missing),
        "fixed": fixed,
        "unresolved": unresolved,
        "dry_run": dry_run,
    }


def folder_has_cover(folder: Path) -> bool:
    try:
        return any(
            p.suffix.lower() in _IMAGE_SUFFIXES for p in folder.iterdir() if p.is_file()
        )
    except OSError:
        return False


def ensure_local_cover(folder: Path, title: str, author: str) -> Path | None:
    """Write `cover.jpg` into a freshly organised book folder.

    Called before the ABS scan so the book appears *with* its artwork rather
    than appearing blank and changing later. Returns None whenever anything is
    missing or fails — a cover is never worth failing an import over.
    """
    if folder_has_cover(folder):
        return None
    url = find_cover_url(title, author)
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        logger.exception("cover download failed for %r", title)
        return None
    if len(resp.content) < _MIN_COVER_BYTES:
        # An error page or a 1px placeholder, not artwork.
        return None
    dest = folder / "cover.jpg"
    try:
        dest.write_bytes(resp.content)
    except OSError:
        logger.exception("could not write cover for %r", title)
        return None
    logger.info("cover saved for %r", title)
    return dest
