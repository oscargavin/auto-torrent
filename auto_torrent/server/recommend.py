"""Claude-powered audiobook recommendations.

generate() asks Claude (via claude_agent_sdk structured output, subscription
auth) for books a profile would enjoy, given their finished books. It returns
bare title/author/reason; build_recommendations() then hydrates each into a
full BookCard (square cover, narrators, …) via audnex.hydrate, dropping any that
resolve to nothing — so a hallucinated title is silently filtered out.

Cost control: per-(profile + history) results are cached, so Claude only runs
when the listening history changes or a refresh is requested. The history hash
*is* the cache key, so finishing a new book naturally invalidates it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query
from pydantic import BaseModel

from ..audnex import hydrate, match_key
from ..types import BookCard

logger = logging.getLogger("atb.recommend")

REC_MODEL = "claude-sonnet-4-6"
DEFAULT_N = 12
CACHE_TTL_S = 7 * 24 * 3600  # re-generate weekly even if history is unchanged
# Structured output needs a few turns (generate → coerce to JSON); 1 hits
# error_max_turns before any output. A flat schema (no $ref/$defs, unlike
# pydantic's model_json_schema) is what the CLI's structured output expects.
REC_MAX_TURNS = 6
REC_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "author": {"type": "string"},
                },
                "required": ["title", "author"],
            },
        }
    },
    "required": ["recommendations"],
}

SYSTEM_PROMPT = """You are an expert audiobook recommender for a family's personal library app.

Given the books a person has finished, recommend audiobooks they're likely to love next. Rules:
- Recommend real, well-known books that exist as audiobooks (so they can be found and downloaded).
- Match the person's demonstrated taste (genre, tone, themes, authors, series) — but include some variety, not just more of the exact same.
- If they finished part of a series, the next book in that series is a strong pick.
- Never recommend a book in their finished list or the exclude list.
- Use the author's most common English name spelling and the canonical book title (no "unabridged"/narrator suffixes)."""


class Rec(BaseModel):
    title: str
    author: str


class RecList(BaseModel):
    recommendations: list[Rec]


def _book_lines(books: list[dict]) -> str:
    return "\n".join(f"- {b.get('title', '')} by {b.get('author', '')}".rstrip() for b in books)


def _build_prompt(finished: list[dict], exclude: list[str], n: int) -> str:
    parts = [f"Recommend {n} audiobooks."]
    parts.append("\nBooks they've finished:\n" + (_book_lines(finished) or "- (none yet)"))
    if exclude:
        parts.append("\nDo NOT recommend any of these:\n" + "\n".join(f"- {t}" for t in exclude))
    if not finished:
        parts.append(
            "\nThey have no history yet — recommend a varied set of acclaimed, popular audiobooks across genres."
        )
    return "\n".join(parts)


async def generate(
    finished: list[dict],
    exclude: list[str] | None = None,
    n: int = DEFAULT_N,
    model: str = REC_MODEL,
) -> list[Rec]:
    """Ask Claude for recommendations. Returns [] on any non-success result."""
    prompt = _build_prompt(finished, exclude or [], n)
    options = ClaudeAgentOptions(
        model=model,
        max_turns=REC_MAX_TURNS,
        system_prompt=SYSTEM_PROMPT,
        output_format={"type": "json_schema", "schema": REC_SCHEMA},
    )
    recs: list[Rec] = []
    subtype: str | None = None
    # Drain the generator fully — breaking early makes the SDK's async generator
    # raise on close ("aclose(): asynchronous generator is already running").
    async for msg in query(prompt=prompt, options=options):
        # ResultMessage is the only message carrying structured_output.
        if not hasattr(msg, "structured_output"):
            continue
        subtype = getattr(msg, "subtype", None)
        if subtype == "success" and msg.structured_output:
            recs = RecList.model_validate(msg.structured_output).recommendations
    if not recs and subtype:
        logger.warning("recommendation generation ended without recs: %s", subtype)
    return recs


def card_to_dict(card: BookCard) -> dict:
    """BookCard → a JSON-ready dict (tuples flattened to lists). The card's own
    `description` is what the app shows — no LLM-generated reason (saves tokens)."""
    d = asdict(card)
    d["narrators"] = list(card.narrators)
    d["genres"] = list(card.genres)
    return d


def _history_key(profile_id: str, finished: list[dict], exclude: list[str]) -> str:
    payload = json.dumps(
        {
            "p": profile_id,
            "f": sorted(match_key(b.get("title", ""), b.get("author", "")) for b in finished),
            "x": sorted(exclude),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class RecCache:
    """Tiny JSON-file KV cache (per-profile rec lists). Last-writer-wins is fine
    for a cache; a lost write just costs one regeneration."""

    def __init__(self, path: str | Path, ttl_s: int = CACHE_TTL_S) -> None:
        self.path = Path(path)
        self.ttl = ttl_s

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def get(self, key: str) -> list[dict] | None:
        entry = self._load().get(key)
        if not entry or time.time() - entry["ts"] > self.ttl:
            return None
        return entry["items"]

    def set(self, key: str, items: list[dict]) -> None:
        data = self._load()
        data[key] = {"ts": time.time(), "items": items}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data))


async def build_recommendations(
    profile_id: str,
    finished: list[dict],
    *,
    exclude: list[str] | None = None,
    n: int = DEFAULT_N,
    refresh: bool = False,
    region: str = "uk",
    cache: RecCache | None = None,
) -> list[dict]:
    """Cached → generate → hydrate (drop misses) → ordered, deduped cards."""
    exclude = exclude or []
    key = _history_key(profile_id, finished, exclude)
    if cache and not refresh:
        cached = cache.get(key)
        if cached is not None:
            return cached

    recs = await generate(finished, exclude, n)

    # Dedup before hydrating so we don't pay for the same book twice.
    seen: set[str] = set()
    unique: list[Rec] = []
    for r in recs:
        k = match_key(r.title, r.author)
        if k not in seen:
            seen.add(k)
            unique.append(r)

    cards = await asyncio.gather(
        *(asyncio.to_thread(hydrate, r.title, r.author, region) for r in unique)
    )
    items = [card_to_dict(card) for card in cards if card is not None]

    if cache:
        cache.set(key, items)
    return items
