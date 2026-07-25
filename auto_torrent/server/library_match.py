"""Is a book already in the Audiobookshelf library?

The app runs the same check before it creates a job, which catches the common
case instantly and for free. This one is the authority: it runs after the agent
has resolved a loose request ("something like Project Hail Mary") into a real
title, which is the only point at which we know what is actually about to be
downloaded — and it is the only check a second device, or an SMS request, goes
through at all.

Deliberately strict, for the same reason as the client's copy: a false positive
refuses a download someone genuinely wants. Normalised equality only, never
containment — "Dune" must not match "Dune Messiah".
"""

from __future__ import annotations

import re

# Library titles carry subtitles nobody types; the part before the separator is
# the book.
_SUBTITLE = re.compile(r"[:–—-]\s.*$")
_NOISE = re.compile(
    r"\b(unabridged|abridged|audiobook|audio book|a novel)\b", re.IGNORECASE
)
_ARTICLE = re.compile(r"^(the|a|an)\s+")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def normalise(raw: str) -> str:
    text = _NOISE.sub("", (raw or "").lower())
    text = _NON_WORD.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return _ARTICLE.sub("", text).strip()


def normalise_title(raw: str) -> str:
    return normalise(_SUBTITLE.sub("", raw or ""))


def find_existing(items: list[dict], title: str, author: str = "") -> dict | None:
    """The library item matching ``title``/``author``, or None."""
    wanted = normalise_title(title)
    # Below this a title is too generic to be evidence.
    if len(wanted) < 4:
        return None
    wanted_author = normalise(author)

    for item in items:
        meta = ((item.get("media") or {}).get("metadata")) or {}
        if normalise_title(meta.get("title") or "") != wanted:
            continue
        known_author = normalise(meta.get("authorName") or "")
        # Same title by a different author is a different book — but only
        # decisive when both sides actually name one.
        if wanted_author and known_author:
            if (
                known_author != wanted_author
                and wanted_author not in known_author
                and known_author not in wanted_author
            ):
                continue
        return item
    return None
