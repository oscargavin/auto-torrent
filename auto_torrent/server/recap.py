"""Claude-powered spoiler-safe chapter recaps ("previously on…").

build_recap() asks Claude for a two-to-three-sentence recap of a published book
UP TO the listener's current chapter, so someone re-opening an audiobook after a
fortnight can pick the thread back up. The model recaps from its own knowledge
of the published work (the server has no transcript), so it must refuse — via
``known: false`` — whenever it isn't confident it knows this exact book; the
client then shows only its local "last heard N days ago" line. A wrong recap is
worse than none.

Cost control mirrors recommend.py: results are cached per (title, author,
chapter index), so Claude runs once per chapter per book, ever — the cache is
shared by every family profile because the recap has no personal content.
"""

from __future__ import annotations

import hashlib
import json
import logging

from claude_agent_sdk import ClaudeAgentOptions, query
from pydantic import BaseModel

from .recommend import RecCache

logger = logging.getLogger("atb.recap")

RECAP_MODEL = "claude-sonnet-4-6"
# Recaps describe the published book, which does not change — cache ~forever.
RECAP_CACHE_TTL_S = 365 * 24 * 3600
RECAP_MAX_TURNS = 6
RECAP_SCHEMA = {
    "type": "object",
    "properties": {
        "known": {"type": "boolean"},
        "recap": {"type": "string"},
    },
    "required": ["known", "recap"],
}

SYSTEM_PROMPT = """You write "previously on…" recaps for audiobook listeners returning to a book after weeks away.

Rules:
- Only recap books you actually know well from your training. If you are not confident you know THIS exact book's plot, set known=false and recap="" — never guess or improvise. A wrong recap is worse than none.
- The listener is PART-WAY through: recap only events BEFORE their current chapter. Absolutely no spoilers from that chapter onwards — no foreshadowing, no "little did they know".
- Two to three sentences, plain and warm, naming the main characters and where the story stands. Write as a reminder for someone who has read this far, not a synopsis for a newcomer.
- If the chapter position is too early for a meaningful recap (the first chapter or two), set known=false."""


class RecapResult(BaseModel):
    known: bool
    recap: str


def _prompt(title: str, author: str, chapter_index: int, chapter_title: str, chapters_total: int) -> str:
    place = f"chapter {chapter_index + 1}"
    if chapters_total:
        place += f" of {chapters_total}"
    if chapter_title:
        place += f' (titled "{chapter_title}")'
    by = f" by {author}" if author else ""
    return (
        f'The listener is part-way through the audiobook "{title}"{by}, currently inside {place}.\n'
        "Write the recap of the story so far, up to but not including that chapter."
    )


def recap_key(title: str, author: str, chapter_index: int) -> str:
    payload = json.dumps(
        {"t": title.strip().lower(), "a": author.strip().lower(), "c": chapter_index},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


async def generate(
    title: str,
    author: str,
    chapter_index: int,
    chapter_title: str,
    chapters_total: int,
    model: str = RECAP_MODEL,
) -> str | None:
    """Ask Claude for a recap. None = unknown book / refusal / any failure."""
    options = ClaudeAgentOptions(
        model=model,
        max_turns=RECAP_MAX_TURNS,
        system_prompt=SYSTEM_PROMPT,
        output_format={"type": "json_schema", "schema": RECAP_SCHEMA},
    )
    result: RecapResult | None = None
    subtype: str | None = None
    # Drain fully — breaking early makes the SDK's generator raise on close.
    async for msg in query(
        prompt=_prompt(title, author, chapter_index, chapter_title, chapters_total),
        options=options,
    ):
        if not hasattr(msg, "structured_output"):
            continue
        subtype = getattr(msg, "subtype", None)
        if subtype == "success" and msg.structured_output:
            result = RecapResult.model_validate(msg.structured_output)
    if result is None and subtype:
        logger.warning("recap generation ended without output: %s", subtype)
    if result is None or not result.known:
        return None
    text = result.recap.strip()
    return text or None


async def build_recap(
    title: str,
    author: str,
    chapter_index: int,
    chapter_title: str = "",
    chapters_total: int = 0,
    *,
    cache: RecCache | None = None,
) -> str | None:
    """Cached → generate. Refusals are cached too (as empty), so an unknown book
    doesn't re-run Claude on every open."""
    key = recap_key(title, author, chapter_index)
    if cache:
        cached = cache.get(key)
        if cached is not None:
            # Stored as a one-element list to fit RecCache's list-shaped values;
            # [] is a cached refusal.
            return cached[0]["recap"] if cached else None

    recap = await generate(title, author, chapter_index, chapter_title, chapters_total)
    if cache:
        cache.set(key, [{"recap": recap}] if recap else [])
    return recap
