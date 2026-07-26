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
from typing import Awaitable, Callable, Literal

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
- A pending option with no magnet is a BOOK the user chose from your suggestions, not a copy. Search for it as a fresh request, then continue as normal — including asking which edition if the copies differ.

ANSWERING RATHER THAN FETCHING.
Not every message is a request for a book. "What's Piranesi about?", "who
narrates it?", "which of those is shortest?", "do I already have Mistborn?",
"why that one?" — these want an answer, not a download. Use `reply` for them:
say the thing, in a sentence or two, and end. Never run a search to answer a
question you already know the answer to, and never fall back to "couldn't find
that one" when they didn't ask you to find anything.

Use `search_library` when the question is about what they own, and before
offering suggestions so you don't recommend a book already on the shelf.

End as soon as you have committed, asked, or replied. Don't keep tool-calling
after."""

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

# Appended for the in-app conversation, where the answer comes back as a tap on
# a card rather than as a text message. Asking is cheap here, so the SMS-era
# "only when truly ambiguous" is too conservative — but a question the user has
# no basis to answer is worse than a decision, so the bar is "the options differ
# in a way they can see", not "more than one result exists".
APP_ASK_CLAUSE = """

THIS IS THE APP, NOT SMS.
The user sees your options as tappable cards with cover art, and answers with
one tap. Asking costs them a second; downloading the wrong thing costs twenty
minutes and clutters a shared family library. So when in doubt, ASK.

There are two separate decisions, and you may ask about each in turn.

1. WHICH BOOK. Ask whenever the request does not name one exact book:
   - A recommendation or comparison: "something like Name of the Wind",
     "a gripping thriller", "something funny for a long drive". Offer 3-4
     specific books that fit, each with a `note` saying why it fits — this is
     the request type where picking silently is worst, because they were asking
     you to suggest, not to decide.
   - An author with several well-known works, a series without a position, or
     a title that belongs to more than one book.
   Only skip this when they named a specific book, or explicitly handed you the
   choice ("surprise me", "you pick"). Then choose and say why.

2. WHICH EDITION. Once the book is settled, ask whenever the copies differ in a
   way a listener would notice and the user has not already told you their
   preference:
   - Different narrators — name them, and say the accent or country when it is
     a real difference for that book ("British narrator", "American accent").
     Narration is the single biggest quality difference between two copies of
     the same audiobook.
   - Full-cast or dramatised productions (GraphicAudio, BBC dramatisations,
     Audible full-cast) versus a standard single-narrator reading. These are
     very different listening experiences, so never silently choose between them.
   - Unabridged versus abridged.
   - One book versus a collection, box set or whole series.
   Use analyze_cover on the top candidates when narrator fields are empty —
   naming the narrator is usually the whole point of this question.

Do NOT ask about things they cannot meaningfully answer: file size, bitrate,
release group, or two copies that differ only in seeders. Pick the best.

When you ask, give 2-4 options, and put a SHORT `note` on each saying what
makes it different — "unabridged, Stephen Fry, British" or "GraphicAudio
full-cast dramatisation" or "the whole trilogy, 4 GB". The note is the only
thing distinguishing two rows with the same title, so never leave it empty and
never repeat the title inside it.

Put your one line of context in the tool's `question` argument, not in a
separate message.

FOLLOW-UPS. The conversation continues, and browsing is a legitimate way to
use it — they do not have to buy on the first list. Earlier turns are above,
including every option you offered and which one they picked, so resolve a
follow-up against that rather than treating it as a fresh request.

Expect all of these:
- Refining by position: "the second one", "that first one but shorter". You can
  see the numbered list you offered; resolve it yourself and never ask them to
  repeat the title.
- Going a level deeper: "I like the idea of the Murderbot one, more like that?"
  That is a NEW suggestion round anchored on the book they named — offer 3-4
  fresh titles closer to it, and do not re-offer anything already on a list
  above. Say in one line what you narrowed towards ("leaning into the wry
  first-person ones").
- Rejecting the whole list: "none of these", "something lighter". Offer a
  different set, on a different axis; repeating yourself with one swap is the
  failure mode here.
- Switching entirely. If they name a new book, drop the thread of suggestions
  and just find it.

They may go several rounds before choosing anything. That is the feature
working, not a loop to escape — keep offering until they pick one or ask you
to decide."""


# How many library matches a search returns. Enough to answer "what have I got
# by Sanderson?" honestly, few enough not to flood the context.
LIBRARY_MATCH_LIMIT = 12


async def _search_library(settings: Settings, query: str) -> list[dict]:
    """Substring match over the family's shelf, on title and author.

    Deliberately looser than `library_match.find_existing`, which exists to
    decide whether to skip a download and so must not produce false positives.
    This one answers questions, where a near-miss in the list is useful and a
    miss is not.
    """
    from .audiobookshelf import ABSClient

    items = await ABSClient(settings).list_items(settings.abs_library_id)
    needle = query.casefold().strip()
    out: list[dict] = []
    for item in items:
        meta = ((item.get("media") or {}).get("metadata")) or {}
        title = meta.get("title") or ""
        author = meta.get("authorName") or ""
        if needle in title.casefold() or needle in author.casefold():
            out.append({"title": title, "author": author})
            if len(out) >= LIBRARY_MATCH_LIMIT:
                break
    return out


def _system_prompt(allow_ask: bool, is_app: bool) -> str:
    """One place that decides what the agent is allowed to do this turn.

    Three channels, three shapes: the app can ask and gets the follow-up rules,
    SMS can ask but only in text, and the bare jobs path cannot ask at all.
    """
    if is_app:
        return SYSTEM_PROMPT + APP_ASK_CLAUSE
    return SYSTEM_PROMPT if allow_ask else SYSTEM_PROMPT + NO_ASK_CLAUSE


@dataclass
class AgentOutcome:
    #: `replied` is a turn that answered a question instead of fetching
    #: anything — "what's Piranesi about?", "do I already have Mistborn?".
    #: Without it, ending without committing fell through to the not-found
    #: fallback, so a perfectly good question got "Couldn't find that one —
    #: try the full title and author."
    kind: Literal["committed", "asked", "replied", "no_results", "error"]
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
    on_ask: Callable[[str, str, list[dict]], Awaitable[None]] | None = None,
    history: list[tuple[str, str]] | None = None,
) -> AgentOutcome:
    """Run the concierge agent for one request.

    `allow_ask` is about the CALLER's ability to deliver an answer, not about
    which channel it is — SMS keys pending options by a stable phone number and
    the legacy /chat route by a client-supplied session_id, so both can resolve
    a pick. The bare jobs worker passes a fresh uuid per job into a process-local
    store in the wrong process, so `ask_user_to_pick` there wrote state nothing
    would ever read and left the user a numbered list they could not answer.

    `on_ask` is the modern answer to that: the thread worker passes a callback
    that persists the options in Redis and appends a choice message, which the
    app renders as tappable cards. Supplying it implies allow_ask.

    `history` is (role, text) for the earlier turns of a conversation, oldest
    first. Only the thread channel has any; SMS and the bare jobs path pass
    None and behave exactly as before.
    """
    allow_ask = allow_ask or on_ask is not None
    state: dict = {"outcome": None}
    # The agent searches more than once when the first pass doesn't satisfy it.
    # Observed live: three rounds emitting "Found 10 copies — checking each…"
    # / "Ranking them…" verbatim each time, which reads as a stuck loop rather
    # than as three attempts. The round number lets the narration say which.
    search_attempts = {"n": 0}

    # ---- Tools ----

    def _already_ended() -> dict | None:
        """Refuse a second terminal tool call in one turn.

        The SDK loop `continue`s once an outcome is set rather than stopping, so
        nothing prevented the agent from calling `ask_user_to_pick` and then
        `commit_download` — observed live: it asked which edition, then started
        one anyway, and the user was shown a question that had already been
        answered for them by a download they never chose.
        """
        if state["outcome"] is None:
            return None
        return {
            "content": [
                {
                    "type": "text",
                    "text": "this turn has already ended; stop calling tools",
                }
            ]
        }

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
        description=(
            "Ask the user to choose, and end this turn. `kind` is 'book' when the "
            "options are different BOOKS to pick between, or 'edition' when they are "
            "different copies of the same book — answering the first runs a search, "
            "answering the second starts a download, and the app says so. "
            "`question` is one short "
            "line of context shown above the options ('Two versions of this one — "
            "which?'). Each option: {label, magnet, title, author, narrator, "
            "format, size, cover_url, note}. `note` is a SHORT phrase saying what "
            "makes this option different from the others ('unabridged, Stephen "
            "Fry'); it is what the user actually chooses on. Give 2-4 options."
        ),
        input_schema={"kind": str, "question": str, "options": list},
    )
    async def ask_user_to_pick(args: dict) -> dict:
        if (ended := _already_ended()) is not None:
            return ended
        options = args.get("options") or []
        if not options:
            return {"content": [{"type": "text", "text": "error: no options"}]}
        # Carried on the tool call rather than left to a preceding send_sms.
        # Observed live: the agent asked with no preamble at all, so the user
        # got a bare list of near-identical rows and no reason for the question.
        # A prompt instruction can be ignored; a required argument cannot.
        question = (args.get("question") or "").strip() or "Which one?"
        # Falls back to `edition`, the costlier reading: mislabelling a search
        # as a download is a smaller error than the reverse, where a tap the
        # user thought was cheap starts twenty minutes of work.
        kind = "book" if str(args.get("kind") or "").lower() == "book" else "edition"

        # The app channel delivers the question as tappable cards and persists
        # the magnets server-side, so it takes the whole payload. SMS can only
        # send text, so it falls through to the numbered list below.
        if on_ask is not None:
            await on_ask(question, kind, options)
            state["outcome"] = AgentOutcome(kind="asked", options=options)
            return {"content": [{"type": "text", "text": "asked user; conversation ended"}]}

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

        lines = [question]
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
        name="reply",
        description=(
            "Answer the user and end the turn. Use whenever they asked something "
            "rather than asked for something — what a book is about, who narrates "
            "it, what you'd recommend and why, or a follow-up to your own last "
            "message. Do NOT search first; answer from what you know."
        ),
        input_schema={"text": str},
    )
    async def reply(args: dict) -> dict:
        if (ended := _already_ended()) is not None:
            return ended
        text = (args.get("text") or "").strip()
        if not text:
            return {"content": [{"type": "text", "text": "error: empty reply"}]}
        try:
            await asyncio.to_thread(sms.send, phone, text)
        except Exception as e:  # noqa: BLE001
            return {"content": [{"type": "text", "text": f"error: {e}"}]}
        state["outcome"] = AgentOutcome(kind="replied", message=text)
        return {"content": [{"type": "text", "text": "replied; conversation ended"}]}

    @tool(
        name="search_library",
        description=(
            "Search the family's existing Audiobookshelf library. Returns "
            "{matches: [{title, author}]}. Use before offering suggestions so you "
            "don't suggest something they already own, and to answer 'do I have "
            "X?' or 'what have I got by Y?'."
        ),
        input_schema={"query": str},
    )
    async def search_library(args: dict) -> dict:
        query = (args.get("query") or "").strip()
        if not query:
            return {"content": [{"type": "text", "text": json.dumps({"matches": []})}]}
        try:
            matches = await _search_library(settings, query)
        except Exception as e:  # noqa: BLE001
            # A library that can't be reached must not fail the turn — the
            # answer degrades to "I can't see your shelf right now", which is
            # far better than the whole request erroring.
            logger.warning("library search failed: %r", e)
            return {
                "content": [
                    {"type": "text", "text": json.dumps({"error": "library unavailable"})}
                ]
            }
        return {"content": [{"type": "text", "text": json.dumps({"matches": matches})}]}

    @tool(
        name="commit_download",
        description="Start the BG download. `primary` and each `fallbacks` entry: {magnet, title, author, narrator, format}. Include narrator and format on `primary` whenever the search result or analyze_cover gave them — they're shown to the user so they can see which edition was chosen. ALWAYS include 1-2 fallbacks when you have viable alternates — the polling layer uses them if the primary stalls.",
        input_schema={"primary": dict, "fallbacks": list},
    )
    async def commit_download(args: dict) -> dict:
        if (ended := _already_ended()) is not None:
            return ended
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
        reply,
        search_library,
        commit_download,
    ]
    if allow_ask:
        tools.insert(4, ask_user_to_pick)

    server = create_sdk_mcp_server(name="atb", tools=tools)

    user_prompt_parts: list[str] = []
    if history:
        # Oldest first, so the last line before the new request is the most
        # recent thing that happened — which is what a follow-up refers to.
        rendered = "\n".join(f"{role}: {text}" for role, text in history)
        user_prompt_parts.append(f"Earlier in this conversation:\n{rendered}\n")
    user_prompt_parts.append(f"User said: {raw_query!r}")
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
        "mcp__atb__reply",
        "mcp__atb__search_library",
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
                system_prompt=_system_prompt(allow_ask, on_ask is not None),
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
