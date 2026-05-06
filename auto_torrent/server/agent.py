"""Agentic SMS audiobook concierge using claude_agent_sdk (subscription auth).

Tools are closures that bind the per-request phone number, settings, and SMS
client. The agent runs once per inbound SMS and exits via either
`commit_download` (with prioritised fallbacks) or `ask_user_to_pick`. The
deterministic poll/organise/scan layer lives in worker.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Literal

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, query, tool

from .. import abb
from ..cli import (
    _enrich_results,
    _execute_download_bg,
    _fan_out_search,
    _probe_seeds_batch,
    _read_state,
    _resolve_status,
)
from ..config import MIN_SCORE, get_proxy
from ..openlibrary import lookup_book
from ..scoring import quick_score, score_and_sort
from ..types import BookMetadata, ScoredResult, SearchResult
from .llm import store_pending_results
from .settings import Settings
from .sms import SMSClient
from .vision import analyze_cover as _analyze_cover

logger = logging.getLogger("atb.agent")

AGENT_MODEL = "claude-sonnet-4-6"
AGENT_MAX_TURNS = 12

SYSTEM_PROMPT = """You are a warm, concise SMS audiobook concierge. The user texts you a book they want; you find it, start the download, and confirm. Messages are SMS — under 160 chars where practical, no markdown, no emojis other than a single ✓ for completion (which the polling layer sends, not you).

Tools:
- search_audiobookbay(query, limit=5): returns ranked ABB results.
- analyze_cover(cover_url): vision OCR. Returns {title, author, narrator} ONLY for what's printed on the image. Use to confirm narrator when the result's narrator field is empty.
- probe_peers(magnet): live seeder count. Use to avoid dead torrents when you have a choice.
- send_sms(text): single SMS to the user.
- ask_user_to_pick(options): numbered list, ends the turn. Each option: {label, magnet, title, author, narrator}. Use only when truly ambiguous.
- commit_download(primary, fallbacks): start the BG download and end. primary/fallbacks: {magnet, title, author}. Include 1–2 fallbacks whenever you have viable alternates — they are the polling layer's safety net for stalls.

Decision flow:
1. Search with the user's text. If obviously shorthand or typo'd, you may search one corrected variant.
2. Identify the user's intended book/author from the query.
3. Pick the best result. Prefer: correct book/series number > unabridged > standard over dramatized > M4B over MP3 > higher score > more peers.
4. If the user mentioned a narrator OR a result has narrator info that disagrees with another otherwise-identical result, verify with analyze_cover on the top 1–2 covers.
5. If results genuinely disagree on a way that matters AND the user gave no preference → ask_user_to_pick (with up to 4 options).
6. Otherwise → send_sms an announce, then commit_download.

Announce format (send_sms before commit_download):
- With narrator KNOWN from the search result or analyze_cover:
    Found "<title>" by <author>, narrated by <narrator>. Downloading now…
- Without confirmed narrator:
    Found "<title>" by <author>. Downloading now…
- Never invent narrator, ETA, or runtime. Don't say "(~5 min)" — you don't know.

When NOTHING matches:
- send_sms: "Couldn't find <query>. Try the full title or author?"
- Then end without commit/ask.

Pending options:
- If "Pending options" are present in the user prompt, treat the user's message as a pick from those options. Resolve to the magnet they meant and commit_download. Don't re-search.

End the conversation as soon as you have committed or asked. Don't keep tool-calling after."""


@dataclass
class AgentOutcome:
    kind: Literal["committed", "asked", "no_results", "error"]
    download: dict | None = None
    fallbacks: list[dict] = field(default_factory=list)
    options: list[dict] = field(default_factory=list)
    display: str = ""
    title: str = ""
    author: str = ""
    message: str = ""


def _truncate(text: str, n: int = 220) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _scored_to_payload(s: ScoredResult, idx: int) -> dict:
    r = s.result
    return {
        "index": idx,
        "title": r.title,
        "author": r.author or "",
        "narrator": r.narrator or "",
        "format": (f"{r.format} {r.bitrate}" if r.bitrate else r.format) or "",
        "size": r.file_size or "",
        "abridged": r.abridged,
        "language": r.language or "",
        "posted": r.posted or "",
        "score": s.score,
        "magnet": r.magnet,
        "cover_url": r.cover_url or "",
        "description": _truncate(r.description) if r.description else "",
    }


def _search_pipeline_sync(raw_query: str, limit: int) -> dict:
    proxy = get_proxy()
    if proxy:
        abb.configure(proxy=proxy)

    book: BookMetadata | None = None
    try:
        book = lookup_book(raw_query)
    except Exception:
        book = None

    if book is None:
        book = BookMetadata(title=raw_query, author="")

    raw_results = _fan_out_search(book, raw_query=raw_query)
    if not raw_results:
        return {"book": _book_to_dict(book), "results": []}

    max_enrich = max(limit * 2, 6)
    if len(raw_results) > max_enrich:
        raw_results.sort(key=lambda r: quick_score(r, book), reverse=True)
        raw_results = raw_results[:max_enrich]

    enriched = _enrich_results(raw_results)
    scored = score_and_sort(enriched, book, prefer_narrator=None, min_score=MIN_SCORE)

    if not scored:
        scored = [ScoredResult(result=r, score=50) for r in enriched if r.magnet][:limit]

    scored = scored[:limit]
    return {
        "book": _book_to_dict(book),
        "results": [_scored_to_payload(s, i) for i, s in enumerate(scored)],
    }


def _book_to_dict(book: BookMetadata) -> dict:
    return {
        "title": book.title,
        "author": book.author or "",
        "series": book.series,
        "cover_id": book.cover_id,
    }


async def run_agent(
    raw_query: str,
    phone: str,
    settings: Settings,
    sms: SMSClient,
    pending_options: list[dict] | None = None,
) -> AgentOutcome:
    state: dict = {"outcome": None}

    # ---- Tools ----

    @tool(
        name="search_audiobookbay",
        description="Search AudiobookBay for an audiobook. Returns up to `limit` ranked results. Each result has index, title, author, narrator, format, size, score, magnet, cover_url, description excerpt.",
        input_schema={"query": str, "limit": int},
    )
    async def search_audiobookbay(args: dict) -> dict:
        try:
            data = await asyncio.to_thread(
                _search_pipeline_sync,
                args.get("query") or raw_query,
                int(args.get("limit") or 5),
            )
            return {"content": [{"type": "text", "text": json.dumps(data)}]}
        except Exception as e:
            logger.exception("search tool failed")
            return {"content": [{"type": "text", "text": json.dumps({"error": f"{type(e).__name__}: {e}"})}]}

    @tool(
        name="analyze_cover",
        description="Vision OCR on an audiobook cover image URL. Returns {title, author, narrator} based on what's printed on the cover. Use when narrator is missing from a search result.",
        input_schema={"cover_url": str},
    )
    async def analyze_cover(args: dict) -> dict:
        try:
            data = await _analyze_cover(args.get("cover_url", ""))
            return {"content": [{"type": "text", "text": json.dumps(data)}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}]}

    @tool(
        name="probe_peers",
        description="Probe a magnet for live seeders via DHT (~10s). Returns {peers: int}. 0 means likely dead.",
        input_schema={"magnet": str},
    )
    async def probe_peers(args: dict) -> dict:
        magnet = args.get("magnet", "")
        try:
            counts = await asyncio.to_thread(_probe_seeds_batch, [magnet], 10)
            return {"content": [{"type": "text", "text": json.dumps({"peers": counts.get(magnet, 0)})}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": json.dumps({"error": str(e), "peers": 0})}]}

    @tool(
        name="send_sms",
        description="Send a single SMS to the user. Keep under 160 chars where possible. No markdown.",
        input_schema={"text": str},
    )
    async def send_sms(args: dict) -> dict:
        text = (args.get("text") or "").strip()
        if not text:
            return {"content": [{"type": "text", "text": "ignored: empty"}]}
        try:
            await asyncio.to_thread(sms.send, phone, text)
            return {"content": [{"type": "text", "text": "sent"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"error: {e}"}]}

    @tool(
        name="ask_user_to_pick",
        description="Present a numbered list to the user and end this turn. Each option: {label, magnet, title, author, narrator}. Only call when results are genuinely ambiguous after analysis.",
        input_schema={"options": list},
    )
    async def ask_user_to_pick(args: dict) -> dict:
        options = args.get("options") or []
        if not options:
            return {"content": [{"type": "text", "text": "error: no options"}]}

        # Store as 'pending_results' so digit replies in app.py resolve them.
        store_pending_results(phone, [
            {
                "title": o.get("title") or o.get("label", "Unknown"),
                "author": o.get("author", ""),
                "narrator": o.get("narrator", ""),
                "magnet": o.get("magnet", ""),
            }
            for o in options
        ])

        lines = ["Found a few — which?"]
        for i, o in enumerate(options[:4], 1):
            label = o.get("label") or o.get("title", "Unknown")
            extras = []
            if o.get("narrator"):
                extras.append(o["narrator"])
            extra = f" ({', '.join(extras)})" if extras else ""
            lines.append(f"{i}. {label}{extra}")
        lines.append("\nReply with the number.")
        await asyncio.to_thread(sms.send, phone, "\n".join(lines))

        state["outcome"] = AgentOutcome(kind="asked", options=options)
        return {"content": [{"type": "text", "text": "asked user; conversation ended"}]}

    @tool(
        name="commit_download",
        description="Start the BG download. `primary` and each `fallbacks` entry: {magnet, title, author}. ALWAYS include 1-2 fallbacks when you have viable alternates — the polling layer uses them if the primary stalls.",
        input_schema={"primary": dict, "fallbacks": list},
    )
    async def commit_download(args: dict) -> dict:
        primary = args.get("primary") or {}
        fallbacks = args.get("fallbacks") or []

        magnet = primary.get("magnet")
        title = primary.get("title") or "Unknown"
        author = primary.get("author") or ""
        if not magnet:
            return {"content": [{"type": "text", "text": "error: primary.magnet required"}]}

        bg_title = f"{title} - {author}" if author else title
        try:
            download = await asyncio.to_thread(_execute_download_bg, bg_title, magnet, None)
        except Exception as e:
            logger.exception("commit_download failed to start BG process")
            return {"content": [{"type": "text", "text": f"error: {e}"}]}

        clean_fallbacks = [
            {
                "magnet": fb.get("magnet", ""),
                "title": fb.get("title") or title,
                "author": fb.get("author", ""),
            }
            for fb in fallbacks
            if fb.get("magnet")
        ]

        display = f"“{title}”" + (f" by {author}" if author else "")
        state["outcome"] = AgentOutcome(
            kind="committed",
            download=download,
            fallbacks=clean_fallbacks,
            display=display,
            title=title,
            author=author,
        )
        return {"content": [{"type": "text", "text": json.dumps({"id": download.get("id"), "started": True})}]}

    server = create_sdk_mcp_server(
        name="atb",
        tools=[
            search_audiobookbay,
            analyze_cover,
            probe_peers,
            send_sms,
            ask_user_to_pick,
            commit_download,
        ],
    )

    user_prompt_parts = [f"User texted: {raw_query!r}"]
    if pending_options:
        formatted = "\n".join(
            f"{i+1}. {o.get('title','?')} (narrator: {o.get('narrator','?')}, author: {o.get('author','?')})"
            for i, o in enumerate(pending_options)
        )
        user_prompt_parts.append(f"\nPending options from earlier conversation (still valid):\n{formatted}")
    user_prompt_parts.append(
        "\nResolve this. Use tools as needed. Exit via commit_download or ask_user_to_pick."
    )
    user_prompt = "\n".join(user_prompt_parts)

    try:
        async for _ in query(
            prompt=user_prompt,
            options=ClaudeAgentOptions(
                model=AGENT_MODEL,
                max_turns=AGENT_MAX_TURNS,
                system_prompt=SYSTEM_PROMPT,
                mcp_servers={"atb": server},
                allowed_tools=[
                    "mcp__atb__search_audiobookbay",
                    "mcp__atb__analyze_cover",
                    "mcp__atb__probe_peers",
                    "mcp__atb__send_sms",
                    "mcp__atb__ask_user_to_pick",
                    "mcp__atb__commit_download",
                ],
            ),
        ):
            if state["outcome"] is not None:
                # Tool already terminated the agent's job.
                continue
    except Exception as e:
        logger.exception("agent loop crashed")
        return AgentOutcome(kind="error", message=f"{type(e).__name__}: {e}")

    if state["outcome"] is not None:
        return state["outcome"]

    return AgentOutcome(kind="error", message="agent ended without committing or asking")


__all__ = ["run_agent", "AgentOutcome"]
