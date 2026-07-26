"""Real chapter names for books whose files don't carry any.

ABS derives chapters from what the download contained, which for a multi-file
MP3 release means one chapter per file named after the file: *The Name of the
Wind* lands as 94 chapters called `00`–`93`. That is a file listing, not a set
of chapters, and it makes the scrub bar useless.

Audnexus — the same free, key-less source behind the covers and ratings —
publishes Audible's chapter list with start offsets. This module fetches it and
writes it onto the item, under two rules that keep it from doing harm:

1. **Only when it is an improvement.** A release that already has real chapter
   titles keeps them. Audible's own titles are often just "Chapter 1", so
   overwriting good embedded metadata with that would be a downgrade.
2. **Only when the runtimes agree.** The offsets are timed against the Audible
   edition. Applied to a different edition — abridged, a different narrator, a
   re-recording — every chapter mark lands in the wrong place, which is worse
   than no chapters at all.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..audnex import fetch_chapters, find_asin
from .covers import clean_author, title_variants

logger = logging.getLogger("atb.chapters")

# How far the two editions may differ before the offsets are untrustworthy.
# Generous enough to absorb differing intros and credits, tight enough that an
# abridgement or a different recording is rejected.
RUNTIME_TOLERANCE = 0.02
RUNTIME_TOLERANCE_MIN_S = 60

# A chapter title carrying no information: a bare number, a track/part/file
# label, or nothing at all. These are what ABS derives from filenames.
_EMPTY_TITLE = re.compile(
    r"""^\s*(
        \d+                                  # 001
        | (chapter|track|part|file|disc|cd)   # Part 001, Track 5
          \s*[-_.\s]*\d+
        | (chapter|track|part)\s+
          (one|two|three|four|five|six|seven|eight|nine|ten)
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class AudibleChapters:
    runtime_s: float
    #: (start_seconds, title), in order.
    chapters: tuple[tuple[float, str], ...]


def is_placeholder(title: str) -> bool:
    """Does this chapter title tell you anything?"""
    return bool(_EMPTY_TITLE.match(title or "")) or not (title or "").strip()


def worth_replacing(existing: list[dict]) -> bool:
    """Whether an item's current chapters are worth overwriting.

    True when the item has none, or when effectively all of them are
    placeholders. A book with even a handful of real titles is left alone — a
    partially-named list is far more likely to be correct metadata we don't
    understand than something worth destroying.
    """
    if not existing:
        return True
    titles = [str(c.get("title") or "") for c in existing]
    named = sum(1 for t in titles if not is_placeholder(t))
    return named <= max(1, len(titles) // 20)


def runtimes_agree(item_s: float, audible_s: float) -> bool:
    """Are these plausibly the same recording?

    Zero or missing runtimes fail closed: without a length to compare, there is
    no evidence the offsets line up, and applying them blind is exactly the
    failure this guard exists to prevent.
    """
    if item_s <= 0 or audible_s <= 0:
        return False
    allowed = max(audible_s * RUNTIME_TOLERANCE, RUNTIME_TOLERANCE_MIN_S)
    return abs(item_s - audible_s) <= allowed


def lookup(title: str, author: str, region: str = "uk") -> AudibleChapters | None:
    """Audible's chapter list for a book, or None.

    Retries the same title variants as the cover lookup — library titles carry
    subtitles that match nothing.
    """
    who = clean_author(author)
    for variant in title_variants(title):
        try:
            asin = find_asin(variant, who, region)
            if not asin:
                continue
            data = fetch_chapters(asin, region)
        except Exception:  # noqa: BLE001 — chapters are never worth failing over
            logger.exception("chapter lookup failed for %r", variant)
            continue
        if not data:
            continue
        raw = data.get("chapters") or []
        chapters = tuple(
            (float(c.get("startOffsetSec") or 0), str(c.get("title") or "").strip())
            for c in raw
            if c.get("title")
        )
        if not chapters:
            continue
        return AudibleChapters(
            runtime_s=float(data.get("runtimeLengthMs") or 0) / 1000.0,
            chapters=chapters,
        )
    return None


def to_abs_payload(chapters: AudibleChapters, total_s: float) -> list[dict]:
    """Audnexus offsets → the chapter list ABS stores.

    ABS wants an explicit `end` per chapter, so each one runs to the start of
    the next and the last runs to the item's own duration — the item's, not
    Audible's, because a few seconds of difference at the tail should not leave
    a gap or overrun the file.
    """
    # Collapse chapters sharing a start offset before pairing them up, keeping
    # the first — otherwise the duplicate produces a zero-length chapter and
    # dropping *that* silently discards the name Audible listed first.
    ordered: list[tuple[float, str]] = []
    for start, title in sorted(chapters.chapters, key=lambda c: c[0]):
        if ordered and ordered[-1][0] == start:
            continue
        ordered.append((start, title))

    out: list[dict] = []
    for i, (start, title) in enumerate(ordered):
        end = ordered[i + 1][0] if i + 1 < len(ordered) else total_s
        if end <= start:
            continue
        out.append(
            {"id": len(out), "start": round(start, 3), "end": round(end, 3), "title": title}
        )
    return out


async def apply_to_item(abs_client, item_id: str, *, dry_run: bool = False) -> dict:
    """Give one item real chapters, if that is an improvement and safe.

    Returns a verdict rather than raising: this runs at the tail of an import
    that has already succeeded, and a chapter list is never worth failing a
    download over.
    """
    import asyncio

    try:
        item = await abs_client.get_item(item_id)
    except Exception:  # noqa: BLE001
        logger.exception("chapters: could not read item %s", item_id)
        return {"applied": False, "reason": "item_unreadable"}

    media = item.get("media") or {}
    meta = media.get("metadata") or {}
    title = meta.get("title") or ""
    author = meta.get("authorName") or ""

    if not worth_replacing(media.get("chapters") or []):
        return {"applied": False, "reason": "already_named", "title": title}

    found = await asyncio.to_thread(lookup, title, author)
    if not found:
        return {"applied": False, "reason": "not_on_audible", "title": title}

    duration = item_duration_s(item)
    if not runtimes_agree(duration, found.runtime_s):
        # A different edition. Its marks would land in the wrong places, which
        # is worse than the filenames we were going to replace.
        return {
            "applied": False,
            "reason": "runtime_mismatch",
            "title": title,
            "item_s": round(duration),
            "audible_s": round(found.runtime_s),
        }

    payload = to_abs_payload(found, duration)
    if not payload:
        return {"applied": False, "reason": "empty_after_conversion", "title": title}
    if dry_run:
        return {"applied": False, "reason": "dry_run", "title": title, "chapters": len(payload)}

    try:
        await abs_client.update_chapters(item_id, payload)
    except Exception:  # noqa: BLE001
        logger.exception("chapters: write failed for %s", title)
        return {"applied": False, "reason": "write_failed", "title": title}
    logger.info("chapters: %s now has %d named chapters", title, len(payload))
    return {"applied": True, "title": title, "chapters": len(payload)}


async def backfill(abs_client, library_id: str, *, dry_run: bool = True) -> dict:
    """Run the same rules over everything already on the shelf.

    Separate from the import path because it fixes what is already there —
    books added before this existed. Safe to run repeatedly: an item that
    gained real chapters is skipped by `worth_replacing` on the next pass.
    Defaults to a dry run, because the alternative default is rewriting a
    family's whole library on a typo.
    """
    items = await abs_client.list_items(library_id)
    results = {"scanned": len(items), "applied": [], "skipped": {}}
    for item in items:
        verdict = await apply_to_item(abs_client, item["id"], dry_run=dry_run)
        if verdict.get("applied") or verdict.get("reason") == "dry_run":
            results["applied"].append(
                {"title": verdict.get("title"), "chapters": verdict.get("chapters")}
            )
        else:
            results["skipped"].setdefault(verdict["reason"], []).append(verdict.get("title"))
    results["dry_run"] = dry_run
    return results


def item_duration_s(item: dict) -> float:
    media = item.get("media") or {}
    duration = media.get("duration")
    if duration:
        return float(duration)
    # Older payloads put it only on the files.
    return float(
        sum(float(f.get("duration") or 0) for f in (media.get("audioFiles") or []))
    )
