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
from typing import Callable, Literal

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
from .event_types import EVENT_PROGRESS, STAGE_FOUND, STAGE_SEARCHING
from .llm import store_pending_results
from .settings import Settings
from .sms import SMSClient
from .vision import analyze_cover as _analyze_cover

logger = logging.getLogger("atb.agent")

AGENT_MODEL = "claude-sonnet-4-6"
# Raised from 12 with the interpretation step: a vague request ("something
# funny for a long drive") legitimately costs a couple of extra searches before
# it converges, and running out of turns mid-search reads to the user as "not
# found" rather than "gave up".
AGENT_MAX_TURNS = 16

SYSTEM_PROMPT = """You are a warm, concise audiobook concierge for a family's shared library. Someone asks for something to listen to; you work out what they mean, find the best copy, start the download, and confirm.

Keep replies short and plain — one or two sentences, no markdown, no emoji. They appear on a small card or as an SMS, not in a chat window.

Tools:
- search_audiobookbay(query, limit=5): returns ranked ABB results.
- analyze_cover(cover_url): vision OCR. Returns {title, author, narrator} ONLY for what's printed on the image. Use to confirm narrator when the result's narrator field is empty.
- probe_peers(magnet): live seeder count. Use to avoid dead torrents when you have a choice.
- send_sms(text): one short message to the user.
- ask_user_to_pick(options): numbered list, ends the turn. Each option: {label, magnet, title, author, narrator}. Use only when truly ambiguous.
- commit_download(primary, fallbacks): start the download and end. primary/fallbacks: {magnet, title, author, narrator, format}. Put narrator and format on `primary` whenever you know them — the user sees them and that is how they catch a wrong pick. Include 1–2 fallbacks whenever you have viable alternates; the polling layer uses them if the primary stalls.

WORK OUT WHAT THEY MEAN FIRST.
People rarely type an exact title. Resolve the request to a specific book (or a specific short list) BEFORE searching, using what you know about books. Handle at least:
- Exact title, with or without author.
- Typos, phonetic spellings, shorthand ("hitchhikers guide", "PHM").
- Author only ("anything by Brandon Sanderson") → pick their best-known or most-loved work.
- Series position ("the sequel to Mistborn", "book 3 of Wheel of Time", "the next Bosch") → name the actual book yourself.
- Description without a title ("the one about the guy stranded on Mars", "that book where the bees talk") → identify it.
- Vibe or occasion ("something funny for a long drive", "a gripping thriller", "something for my mum") → choose one specific well-regarded audiobook that fits, and say why in your announce.
- Vague or open ("surprise me", "a good sci-fi book") → pick something genuinely good and widely liked. Commit to one; do not ask them to narrow it down.
You may search more than once — a corrected spelling, "title author", or the title alone — when the first search is weak. Two or three searches is fine; do not grind.

CHOOSING BETWEEN RESULTS.
1. It must be the book they actually asked for. A different book by the same author is a miss.
2. Prefer the single requested book over a bundle. "Complete Collection", "Omnibus", "Books 1-5", "Trilogy" and similar contain the right book but are far larger and clutter the library. Take a collection ONLY if the user asked for the series/collection, or if no standalone copy exists — and if you do, say so plainly in the announce so they know what they are getting.
3. Prefer: unabridged > abridged; standard reading > dramatized; M4B > MP3; higher score; more peers.
4. If the user named a narrator, honour it. If two otherwise-identical results disagree on narrator, check the top one or two covers with analyze_cover.
5. Use probe_peers to break a tie or to avoid a copy that looks dead.

COMMITTING.
- send_sms a short announce, then commit_download.
- With a known narrator: Found "<title>" by <author>, narrated by <narrator>. Downloading now.
- Without: Found "<title>" by <author>. Downloading now.
- If you interpreted a loose request, say why in a few words: Grabbing "<title>" by <author> — funny, and it holds up over a long drive. Downloading now.
- If you had to take a collection: say "Only found it in <collection name>, grabbing that."
- Never invent a narrator, runtime, or ETA. You do not know how long it will take.

WHEN NOTHING MATCHES.
- send_sms: Couldn't find <what they asked for>. Try the full title and author?
- Then end without committing.

Pending options:
- If "Pending options" are present in the user prompt, treat the user's message as a pick from those options. Resolve to the magnet they meant and commit_download. Don't re-search.

End as soon as you have committed or asked. Don't keep tool-calling after."""

# Appended when the caller has no way to deliver an answer back to the agent.
# Without it the model still tries to ask, and the "question" lands as a
# progress line nobody can reply to.
NO_ASK_CLAUSE = """

IMPORTANT — this channel has no reply path. You cannot ask the user anything:
there is no ask_user_to_pick tool, and nothing you send will be answered. Never
end your turn with a question.

When the request is loose or the results are ambiguous, decide yourself using
the CHOOSING rules and commit to the best candidate. Say in the announce what
you picked and why, so they can see the call you made. Only skip committing
when nothing plausibly matches."""


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
    # Which edition the agent actually picked. Surfaced on the card so a wrong
    # call — an abridged copy, the wrong narrator — is visible and reportable
    # rather than silently landing in a shared family library.
    narrator: str = ""
    file_format: str = ""


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


def _search_pipeline_sync(
    raw_query: str,
    limit: int,
    on_step: Callable[[str], None] | None = None,
    attempt: int = 1,
) -> dict:
    """Resolve a query to ranked candidates.

    `on_step` narrates the sub-steps. This runs inside asyncio.to_thread and
    the callback reaches the event loop via call_soon_threadsafe, so it is
    safe to call from here. Without it this whole function is one opaque
    ~35s block — measured as the longest remaining silence in the lifecycle.
    """
    def step(msg: str) -> None:
        if on_step is not None:
            try:
                on_step(msg)
            except Exception:  # noqa: BLE001
                # Narration must never be able to fail a search.
                logger.exception("search narration failed")

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

    if book.title and book.title.lower() != raw_query.lower():
        step(f"Looks like “{book.title}”" + (f" by {book.author}" if book.author else "") + "…")

    raw_results = _fan_out_search(book, raw_query=raw_query)
    if not raw_results:
        return {"book": _book_to_dict(book), "results": []}

    max_enrich = max(limit * 2, 6)
    if len(raw_results) > max_enrich:
        raw_results.sort(key=lambda r: quick_score(r, book), reverse=True)
        raw_results = raw_results[:max_enrich]

    # The slow part: one page fetch per candidate. Announce the count first so
    # the wait has a visible shape instead of being dead air.
    #
    # On a repeat round the wording changes: identical text would read as a
    # stutter, and saying "again" is the honest description of what is
    # happening. Rounds past the first also skip the ranking line — on a repeat
    # it is filler, and _narrate collapses a second identical round into a
    # single line, leaving the elapsed clock to carry the time.
    n = len(raw_results)
    if attempt > 1:
        step(f"Searching again — {n} more to check…")
    else:
        step(f"Found {n} cop{'y' if n == 1 else 'ies'} — checking each…")
    enriched = _enrich_results(raw_results)

    if attempt == 1:
        step("Ranking them…")
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


def _narrate(sink: object, stage: str, text: str) -> None:
    """Tell the UI what the agent is doing right now.

    The progress pump only starts once a download commits, so everything
    before that — searching, checking seeders, reading a cover — was a single
    frame followed by a minute of silence on the card. Measured at 67s on a
    real run. These are no-ops for the SMS sink, which has no `emit`.
    """
    emit = getattr(sink, "emit", None)
    if not callable(emit):
        return
    # The narration strings are fixed per tool, and the agent calls a tool as
    # many times as it likes — probing three magnets emitted "Checking who's
    # sharing it…" three times, byte-identical, which reads as a stuck card.
    # Suppress the repeat; the client's elapsed clock carries the passage of
    # time without us having to say anything new.
    #
    # The cursor is stashed on the sink, which is safe only because a sink is
    # built per job (jobs/worker.py constructs a fresh StreamEventBus). A
    # shared or pooled sink would leak one job's last line into the next and
    # silently swallow its first frame.
    if getattr(sink, "_last_narration", None) == text:
        return
    try:
        sink._last_narration = text  # type: ignore[attr-defined]
    except AttributeError:
        pass
    emit(EVENT_PROGRESS, {"stage": stage, "text": text})


async def run_agent(
    raw_query: str,
    phone: str,
    settings: Settings,
    sms: SMSClient,
    pending_options: list[dict] | None = None,
    allow_ask: bool = True,
) -> AgentOutcome:
    """Run the concierge agent for one request.

    `allow_ask` is about the CALLER's ability to deliver an answer, not about
    which channel it is — SMS keys pending options by a stable phone number and
    the legacy /chat route by a client-supplied session_id, so both can resolve
    a pick. The jobs worker passes a fresh uuid per job into a process-local
    store in the wrong process, so `ask_user_to_pick` there wrote state nothing
    would ever read and left the user a numbered list they could not answer.
    """
    state: dict = {"outcome": None}
    # The agent searches more than once when the first pass doesn't satisfy it.
    # Observed live: three rounds emitting "Found 10 copies — checking each…"
    # / "Ranking them…" verbatim each time, which reads as a stuck loop rather
    # than as three attempts. The round number lets the narration say which.
    search_attempts = {"n": 0}

    # ---- Tools ----

    @tool(
        name="search_audiobookbay",
        description="Search AudiobookBay for an audiobook. Returns up to `limit` ranked results. Each result has index, title, author, narrator, format, size, score, magnet, cover_url, description excerpt.",
        input_schema={"query": str, "limit": int},
    )
    async def search_audiobookbay(args: dict) -> dict:
        # Deliberately silent here: the job already opened with "Searching…"
        # and _search_pipeline_sync narrates the resolved title when it differs
        # from what was asked for, which is the only genuinely new information
        # at this point. Narrating the query again just repeated the headline.
        search_attempts["n"] += 1
        try:
            data = await asyncio.to_thread(
                _search_pipeline_sync,
                args.get("query") or raw_query,
                int(args.get("limit") or 5),
                lambda msg: _narrate(sms, STAGE_SEARCHING, msg),
                search_attempts["n"],
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
        _narrate(sms, STAGE_SEARCHING, "Checking the cover for the narrator…")
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
        _narrate(sms, STAGE_SEARCHING, "Checking who's sharing it…")
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
        description="Start the BG download. `primary` and each `fallbacks` entry: {magnet, title, author, narrator, format}. Include narrator and format on `primary` whenever the search result or analyze_cover gave them — they're shown to the user so they can see which edition was chosen. ALWAYS include 1-2 fallbacks when you have viable alternates — the polling layer uses them if the primary stalls.",
        input_schema={"primary": dict, "fallbacks": list},
    )
    async def commit_download(args: dict) -> dict:
        primary = args.get("primary") or {}
        fallbacks = args.get("fallbacks") or []

        magnet = primary.get("magnet")
        title = primary.get("title") or "Unknown"
        author = primary.get("author") or ""
        narrator = primary.get("narrator") or ""
        file_format = primary.get("format") or ""
        if not magnet:
            return {"content": [{"type": "text", "text": "error: primary.magnet required"}]}

        bg_title = f"{title} - {author}" if author else title
        _narrate(sms, STAGE_FOUND, f"Starting “{title}”…")
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
            narrator=narrator,
            file_format=file_format,
        )
        return {"content": [{"type": "text", "text": json.dumps({"id": download.get("id"), "started": True})}]}

    tools = [
        search_audiobookbay,
        analyze_cover,
        probe_peers,
        send_sms,
        commit_download,
    ]
    if allow_ask:
        tools.insert(4, ask_user_to_pick)

    server = create_sdk_mcp_server(name="atb", tools=tools)

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

    allowed_tools = [
        "mcp__atb__search_audiobookbay",
        "mcp__atb__analyze_cover",
        "mcp__atb__probe_peers",
        "mcp__atb__send_sms",
        "mcp__atb__commit_download",
    ]
    if allow_ask:
        allowed_tools.append("mcp__atb__ask_user_to_pick")

    try:
        async for _ in query(
            prompt=user_prompt,
            options=ClaudeAgentOptions(
                model=AGENT_MODEL,
                max_turns=AGENT_MAX_TURNS,
                system_prompt=SYSTEM_PROMPT if allow_ask else SYSTEM_PROMPT + NO_ASK_CLAUSE,
                mcp_servers={"atb": server},
                allowed_tools=allowed_tools,
            ),
        ):
            if state["outcome"] is not None:
                # Tool already terminated the agent's job.
                continue
    except Exception:
        # Typically the claude CLI subprocess dying — an expired subscription
        # token surfaces here as a bare "Command failed with exit code 1".
        # The traceback goes to the log for us; the card gets a sentence that
        # tells the user what to do, since there is nothing they can fix.
        logger.exception("agent loop crashed")
        return AgentOutcome(
            kind="error",
            message="Couldn't reach the book finder just now. Try again shortly.",
        )

    if state["outcome"] is not None:
        return state["outcome"]

    # Reached when the agent stops without committing — overwhelmingly "no
    # match found". The message is user-visible on the card, so it reads as an
    # explanation rather than as a description of our control flow.
    return AgentOutcome(
        kind="error",
        message="Couldn't find that one — try the full title and author.",
    )


__all__ = ["run_agent", "AgentOutcome"]
