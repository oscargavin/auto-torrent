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

import asyncio
import logging
import re
from dataclasses import dataclass

from ..audnex import fetch_chapters, find_asin
from .covers import clean_author, item_title_author, title_variants

logger = logging.getLogger("atb.chapters")

# How far the two editions may differ before the offsets are untrustworthy.
# Generous enough to absorb differing intros and credits, tight enough that an
# abridgement or a different recording is rejected.
RUNTIME_TOLERANCE = 0.02
RUNTIME_TOLERANCE_MIN_S = 60

# Structural words that label a position rather than name one.
_STRUCTURAL = re.compile(
    r"\b(chapter|chapters|track|part|section|file|disc|cd|audio|book|volume|"
    r"unabridged|abridged|prologue\s+to)\b",
    re.IGNORECASE,
)
# Numbers written out, which appear in exactly the same position as digits.
_SPELLED = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty)\b",
    re.IGNORECASE,
)
# What a title has to have left, after the noise, to be telling you anything.
_MIN_MEANINGFUL_LETTERS = 3
# Parenthetical and bracketed editions on the book's own title, which never
# appear in its chapter labels: "Dune (Unabridged)", "…[Headphones]".
_EDITION = re.compile(r"[\(\[][^\)\]]*[\)\]]")

# Above this share of uninformative titles, the list is a file listing wearing
# chapter clothes. Deliberately not 100%: a release with one real "Opening
# Credits" and sixty numbered files is still a file listing, and Dune's three
# part names among forty-eight bare "Chapter N" is the case that made the old
# 5% rule wrong.
REPLACE_ABOVE_PLACEHOLDER_SHARE = 0.8


@dataclass(frozen=True)
class AudibleChapters:
    runtime_s: float
    #: (start_seconds, title), in order.
    chapters: tuple[tuple[float, str], ...]


def is_placeholder(title: str, book_title: str = "") -> bool:
    """Does this chapter title tell you anything?

    Asked by subtraction rather than by pattern: strip the book's own name, the
    structural words, and every number, and see whether anything is left. An
    enumerated list of shapes kept missing real cases — `1a`, `01 Audio CD`,
    `Best Served Cold 01`, `01/15 - The Girl Who Played with Fire` all named
    nothing while matching nothing.

    `book_title` matters more than it looks: repeating the book's name in every
    chapter is one of the commonest ways a release ends up with 16 identically
    labelled "chapters".
    """
    text = (title or "").strip()
    if not text:
        return True
    # Underscores are separators in a filename but word characters to a regex,
    # so `file_03` would keep its "file" and read as meaningful.
    stripped = text.casefold().replace("_", " ")
    book = _EDITION.sub(" ", book_title or "").casefold().strip()
    if book:
        stripped = stripped.replace(book, " ")
    stripped = _STRUCTURAL.sub(" ", stripped)
    stripped = _SPELLED.sub(" ", stripped)
    # Everything that isn't a letter goes, which takes the digits with it — and
    # the stray letter in `1a` with them.
    letters = re.sub(r"[^a-z]", "", stripped)
    return len(letters) < _MIN_MEANINGFUL_LETTERS


def placeholder_share(titles: list[str], book_title: str = "") -> float:
    """What fraction of these titles name nothing. 1.0 for an empty list."""
    if not titles:
        return 1.0
    # Every chapter carrying the same label is a file listing however much text
    # it contains — sixteen rows reading "The Girl with the Dragon Tattoo" tell
    # you exactly as much as sixteen rows reading "001".
    if len(set(t.strip() for t in titles)) == 1:
        return 1.0
    return sum(1 for t in titles if is_placeholder(t, book_title)) / len(titles)


def worth_replacing(existing: list[dict], book_title: str = "") -> bool:
    """Whether an item's current chapters are worth overwriting.

    True when the item has none, or when the overwhelming majority name
    nothing. A book with a real set of titles is left alone: Audible's own are
    frequently just "Chapter 1", so replacing good embedded metadata would be a
    downgrade rather than a fix.
    """
    if not existing:
        return True
    titles = [str(c.get("title") or "") for c in existing]
    return placeholder_share(titles, book_title) >= REPLACE_ABOVE_PLACEHOLDER_SHARE


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
    try:
        item = await abs_client.get_item(item_id)
    except Exception:  # noqa: BLE001
        logger.exception("chapters: could not read item %s", item_id)
        return {"applied": False, "reason": "item_unreadable"}

    media = item.get("media") or {}
    title, author = item_title_author(item)

    if not worth_replacing(media.get("chapters") or [], title):
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
