"""The audiobook concierge behind the app's chat, on claude_agent_sdk
(subscription auth).

An `AgentSession` is one conversation: a claude CLI kept open between turns, so
a follow-up skips the 3-4 s start-up and the agent still has the results it saw
last turn. A turn ends with `commit_download`, `ask_user_to_pick`, or the
agent's own final text, which is its reply.
The deterministic poll/organise/scan layer lives in worker.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from .. import abb
from ..cli import (
    _enrich_results,
    _execute_download_bg,
    _fan_out_search,
    _probe_seeds_batch,
)
from ..config import MIN_SCORE, get_proxy
from ..openlibrary import lookup_book
from ..scoring import quick_score, score_and_sort
from ..types import BookMetadata, ScoredResult
from .covers import item_title_author
from .event_types import EVENT_PROGRESS, STAGE_FOUND, STAGE_SEARCHING
from .library_match import find_existing, normalise_title
from .settings import Settings
from .vision import analyze_cover as _analyze_cover

logger = logging.getLogger("atb.agent")

# Sonnet over Haiku, chosen 9 October 2026 on 15 real requests run twice each:
# about as fast (median 15.3 s vs 17.7 s) and steadier, asking before an
# ambiguous edition rather than picking one. Recaps, recommendations and cover
# reading stay on Haiku.
AGENT_MODEL = "claude-sonnet-5-5"
# Raised from 12 with the interpretation step: a vague request ("something
# funny for a long drive") legitimately costs a couple of extra searches before
# it converges, and running out of turns mid-search reads to the user as "not
# found" rather than "gave up".
AGENT_MAX_TURNS = 16
# Candidates one probe_peers call checks at most, and how long it keeps looking
# for the others once the first has a peer.
PROBE_MAX = 4
PROBE_SETTLE_S = 3.0
PROBE_TIMEOUT_S = 10.0

SYSTEM_PROMPT = """You are a warm, concise audiobook concierge for a family's shared library. Someone asks for something to listen to; you work out what they mean, find the best copy, start the download, and confirm.

Keep replies short and plain — one or two sentences, no markdown, no emoji. They appear in a chat on a phone.

Tools:
- search_audiobookbay(query, limit=5, title, author): returns ranked ABB results, already checked and scored. Once you know the book, pass its exact `title` and `author` too — the search then looks for that book rather than guessing from the query.
- analyze_cover(cover_url): vision OCR. Returns {title, author, narrator} ONLY for what's printed on the image. Use to confirm narrator when the result's narrator field is empty.
- probe_peers(magnets): live seeder counts for one or more magnets, checked together. Pass every candidate you are weighing in ONE call rather than one call each.
- say(text): one short line to the user mid-turn, before a long step. Your final message is your reply; you do not need a tool to answer.
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
You may search more than once — a corrected spelling, "title author", or the title alone — when the first search is weak. Two or three searches is fine; do not grind. Each search takes several seconds while the user waits, so never repeat a search that already found the book, and never re-run it with a trivial rewording. If two searches have not found it, it is not on the site: say so.

CHOOSING BETWEEN RESULTS.
1. It must be the book they actually asked for. A different book by the same author is a miss.
2. Prefer the single requested book over a bundle. "Complete Collection", "Omnibus", "Books 1-5", "Trilogy" and similar contain the right book but are far larger and clutter the library. Take a collection ONLY if the user asked for the series/collection, or if no standalone copy exists — and if you do, say so plainly in the announce so they know what they are getting.
3. Prefer: unabridged > abridged; standard reading > dramatized; M4B > MP3; higher score; more peers.
4. If the user named a narrator, honour it. If two otherwise-identical results disagree on narrator, check the top one or two covers with analyze_cover.
5. Use probe_peers to break a tie or to avoid a copy that looks dead.

COMMITTING.
- say a short announce, then commit_download.
- With a known narrator: Found "<title>" by <author>, narrated by <narrator>. Downloading now.
- Without: Found "<title>" by <author>. Downloading now.
- If you interpreted a loose request, say why in a few words: Grabbing "<title>" by <author> — funny, and it holds up over a long drive. Downloading now.
- If you had to take a collection: say "Only found it in <collection name>, grabbing that."
- Never invent a narrator, runtime, or ETA. You do not know how long it will take.

A DIFFERENT BOOK IS NEVER A MATCH.
If the search does not find the book they asked for, do not download one with
a similar name. "Some Sort of Justice" is not "Ancillary Justice", and a book
by another author is a miss even when the title matches. When you are not sure
a result is their book, ask.

THE SHELF. commit_download refuses a book they already own, including a set
whose books are all on the shelf, and tells you what they have: tell them, with
the narrator. Pass `force: true` only when they asked for another copy on
purpose (a different narrator or edition).

WHEN NOTHING MATCHES.
- Say you couldn't find <what they asked for> on the site. Name any other books
  by the author that are there, and offer to watch for it: "Want me to watch for
  it? I'll grab it when it turns up." Ask this in your own words, not with
  ask_user_to_pick: a tap on a card would search again rather than watch.
- If they say yes, call watch_for, then confirm in one line.
- Then end without committing.

DOWNLOADS. Each past download appears in the conversation as a "[download]"
line saying how it went. For "is it done?" or "I don't see it in my library",
use check_downloads for the latest state, and say what happened and what to
do: a failed copy can be fetched again, and a finished one can take a few
minutes to show up in the app.

Pending options:
- If "Pending options" are present in the user prompt, treat the user's message as a pick from those options. Resolve to the magnet they meant and commit_download. Don't re-search.
- A pending option with no magnet is a BOOK the user chose from your suggestions, not a copy. Search for it as a fresh request, then continue as normal — including asking which edition if the copies differ.

ANSWERING RATHER THAN FETCHING.
Not every message is a request for a book. "What's Piranesi about?", "who
narrates it?", "which of those is shortest?", "do I already have Mistborn?",
"why that one?" — these want an answer, not a download. Answer in a sentence
or two as your final message, and end. Never run a search to answer a
question you already know the answer to, and never fall back to "couldn't find
that one" when they didn't ask you to find anything.

Use `search_library` when the question is about what they own, and before
offering suggestions so you don't recommend a book already on the shelf. It
returns each book's id, narrator, series, length and year, and matches on any
of them.

WHEN THEY REFER TO BOOKS THEY OWN — "the same narrator as my Farseer books",
"the next one in the series I'm on", "is this longer than the last one" — look
them up first instead of guessing. search_library gives the narrator for most
books; `inspect_book` gives one book's full record and reads its cover when no
narrator is recorded. Then search for a copy that matches (put the narrator in
the query), and say plainly when they already own what they asked for or when
no copy has the narrator they want.

End as soon as you have committed, asked, or replied. Don't keep tool-calling
after."""

# The answer comes back as a tap on a card, so asking is cheap — but a question
# the user has no basis to answer is worse than a decision, so the bar is "the
# options differ in a way they can see", not "more than one result exists".
APP_ASK_CLAUSE = """

THE APP.
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
# by Robin Hobb?" honestly (22 books), few enough not to flood the context.
LIBRARY_MATCH_LIMIT = 25


def _library_row(item: dict) -> dict:
    media = item.get("media") or {}
    meta = media.get("metadata") or {}
    title, author = item_title_author(item)
    return {
        "id": item.get("id", ""),
        "title": title,
        "author": author,
        "narrator": meta.get("narratorName") or "",
        "series": meta.get("seriesName") or "",
        "hours": round((media.get("duration") or 0) / 3600, 1),
        "year": meta.get("publishedYear") or "",
    }


# The agent checks the shelf before every download and every suggestion list,
# and each call fetched the whole library (up to 500 items).
LIBRARY_CACHE_S = 60.0
_library_cache: tuple[float, list[dict]] | None = None


async def _library_items(settings: Settings) -> list[dict]:
    global _library_cache
    from .audiobookshelf import ABSClient

    now = time.monotonic()
    if _library_cache is None or now - _library_cache[0] > LIBRARY_CACHE_S:
        _library_cache = (now, await ABSClient(settings).list_items(settings.abs_library_id))
    return _library_cache[1]


async def _search_library(settings: Settings, query: str) -> list[dict]:
    """Substring match over the family's shelf, on title, author, narrator and
    series. The narrator is what "the same narrator as my Farseer books" needs:
    with only title and author, the agent told the user the library did not
    record one.

    Deliberately looser than `library_match.find_existing`, which exists to
    decide whether to skip a download and so must not produce false positives.
    This one answers questions, where a near-miss in the list is useful and a
    miss is not.
    """
    items = await _library_items(settings)
    needle = query.casefold().strip()
    out: list[dict] = []
    for item in items:
        row = _library_row(item)
        haystack = " ".join((row["title"], row["author"], row["narrator"], row["series"]))
        if needle in haystack.casefold():
            out.append(row)
            if len(out) >= LIBRARY_MATCH_LIMIT:
                break
    return out


# Words that make a title name a set rather than one book.
_SET_WORDS = re.compile(
    r"\b(the\s+)?(complete\s+)?(trilogy|series|saga|omnibus|collection|box\s*set|books?\s*\d+\s*-\s*\d+)\b",
    re.I,
)


async def _owned(settings: Settings, title: str, author: str) -> list[dict]:
    """The shelf's copies of a book, or of every book in a set, or [].

    A guard rather than a prompt rule: the agent was told to check the shelf
    first and still started "The Tawny Man Trilogy" with all three books
    already there. One book counts when the title matches; a set counts when at
    least two owned books carry its name.
    """
    try:
        items = await _library_items(settings)
    except Exception:  # noqa: BLE001
        logger.warning("shelf check failed; allowing the download", exc_info=True)
        return []
    match = find_existing(items, title, author)
    if match is not None:
        return [_library_row(match)]
    core = normalise_title(_SET_WORDS.sub(" ", title))
    if not _SET_WORDS.search(title) or len(core) < 4:
        return []
    rows = [_library_row(i) for i in items]
    in_set = [
        r for r in rows
        if core in normalise_title(r["title"]) or core in normalise_title(r["series"])
    ]
    return in_set if len(in_set) >= 2 else []


async def _inspect_book(settings: Settings, item_id: str) -> dict:
    """Everything the library knows about one book. When it records no
    narrator, the cover is read instead, since covers usually print one."""
    from .audiobookshelf import ABSClient
    from .vision import read_cover_image

    abs_client = ABSClient(settings)
    item = await abs_client.get_item(item_id)
    media = item.get("media") or {}
    meta = media.get("metadata") or {}
    book = {
        **_library_row(item),
        "subtitle": meta.get("subtitle") or "",
        "narrators": meta.get("narrators") or [],
        "series_detail": [
            {"name": s.get("name"), "sequence": s.get("sequence")} for s in meta.get("series") or []
        ],
        "publisher": meta.get("publisher") or "",
        "abridged": bool(meta.get("abridged")),
        "genres": meta.get("genres") or [],
        "chapters": len(media.get("chapters") or []),
        "files": len(media.get("audioFiles") or []),
        "description": _truncate(meta.get("descriptionPlain") or meta.get("description") or "", 600),
    }
    if not book["narrator"]:
        try:
            data, media_type = await abs_client.get_cover(item_id)
            book["cover_says"] = await read_cover_image(data, media_type)
        except Exception as e:  # noqa: BLE001
            logger.warning("library cover read failed for %s: %r", item_id, e)
            book["cover_says"] = {}
    return book


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
    # The agent loop died rather than finishing. Whatever the agent said before
    # that was mid-search narration, so the turn still owes the user an answer.
    crashed: bool = False


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
    title: str = "",
    author: str = "",
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

    # The agent has already worked out which book it wants, and it is usually
    # right where the catalogue lookup guesses: "Dune Frank Herbert" resolved to
    # Dune Messiah, and a whole search round went on the wrong book.
    if title and normalise_title(book.title) != normalise_title(title):
        book = BookMetadata(title=title, author=author or "")
    elif title and author and not book.author:
        book = replace(book, author=author)

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
    real run.
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
    # built per turn (threads/worker.py constructs a fresh ThreadSink). A
    # shared sink would leak one turn's last line into the next and silently
    # swallow its first frame.
    if getattr(sink, "_last_narration", None) == text:
        return
    try:
        sink._last_narration = text  # type: ignore[attr-defined]
    except AttributeError:
        pass
    emit(EVENT_PROGRESS, {"stage": stage, "text": text})


# A session restarts after this many turns. Each turn adds its search results
# to the context, so a long conversation grows slower and dearer per message.
SESSION_MAX_TURNS = 8


def _out(value) -> dict:
    """A tool result: text as given, anything else as JSON."""
    return {"content": [{"type": "text", "text": value if isinstance(value, str) else json.dumps(value)}]}


def _build_tools(t: "AgentSession") -> list:
    """The agent's tools, bound once per session. Per-turn state lives on `t`
    and is reset by `AgentSession.turn`, so the same tools serve every turn."""

    async def _probe(magnets: list[str], settle_s: float | None) -> dict[str, int]:
        async with t.probe_lock:
            missing = [m for m in magnets if m not in t.peer_counts]
            if missing:
                t.peer_counts.update(
                    await asyncio.to_thread(_probe_seeds_batch, missing, 10, settle_s=settle_s)
                )
        return {m: t.peer_counts.get(m, 0) for m in magnets}

    def _prefetch_peers(data: dict) -> None:
        magnets = [
            r["magnet"] for r in data.get("results") or [] if r.get("magnet")
        ][:PROBE_MAX]
        magnets = [m for m in magnets if m not in t.peer_counts]
        if not magnets:
            return
        # A full settle window: nobody waits on this probe, and a short one
        # would record 0 for a copy that is slow to find its peers.
        task = asyncio.create_task(_probe(magnets, PROBE_TIMEOUT_S))
        for m in magnets:
            t.probing[m] = task
        task.add_done_callback(lambda done: done.cancelled() or done.exception())

    # ---- Tools ----

    def _already_ended() -> dict | None:
        """Refuse a second terminal tool call in one turn.

        The SDK loop `continue`s once an outcome is set rather than stopping, so
        nothing prevented the agent from calling `ask_user_to_pick` and then
        `commit_download` — observed live: it asked which edition, then started
        one anyway, and the user was shown a question that had already been
        answered for them by a download they never chose.
        """
        if t.outcome is None:
            return None
        return _out("this turn has already ended; stop calling tools")

    @tool(
        name="search_audiobookbay",
        description="Search AudiobookBay for an audiobook. Returns up to `limit` ranked results. Each result has index, title, author, narrator, format, size, score, magnet, cover_url, description excerpt. Pass `title` and `author` once you know exactly which book it is.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
                "title": {"type": "string", "description": "The exact book title, if known."},
                "author": {"type": "string", "description": "Its author, if known."},
            },
            "required": ["query"],
        },
    )
    async def search_audiobookbay(args: dict) -> dict:
        # Deliberately silent here: the job already opened with "Searching…"
        # and _search_pipeline_sync narrates the resolved title when it differs
        # from what was asked for, which is the only genuinely new information
        # at this point. Narrating the query again just repeated the headline.
        query_text = args.get("query") or t.raw_query
        limit = int(args.get("limit") or 5)
        title = str(args.get("title") or "").strip()
        author = str(args.get("author") or "").strip()
        # A repeat of a search already run this turn costs ~10 s of page fetches
        # for the same answer. Observed live: the same query three times.
        key = (query_text.casefold().strip(), limit, title.casefold(), author.casefold())
        if key in t.search_cache:
            return _out(t.search_cache[key])
        t.search_attempts += 1
        try:
            data = await asyncio.to_thread(
                _search_pipeline_sync,
                query_text,
                limit,
                lambda msg: _narrate(t.sink, STAGE_SEARCHING, msg),
                t.search_attempts,
                title,
                author,
            )
            text = json.dumps(data)
            t.search_cache[key] = text
            _prefetch_peers(data)
            return _out(text)
        except Exception as e:
            logger.exception("search tool failed")
            return _out({"error": f"{type(e).__name__}: {e}"})

    @tool(
        name="analyze_cover",
        description="Vision OCR on an audiobook cover image URL. Returns {title, author, narrator} based on what's printed on the cover. Use when narrator is missing from a search result.",
        input_schema={"cover_url": str},
    )
    async def analyze_cover(args: dict) -> dict:
        _narrate(t.sink, STAGE_SEARCHING, "Checking the cover for the narrator…")
        try:
            data = await _analyze_cover(args.get("cover_url", ""))
            return _out(data)
        except Exception as e:
            return _out({"error": str(e)})

    @tool(
        name="probe_peers",
        description="Probe magnets for live seeders via DHT, all at once (up to ~10s in total). Returns {peers: {magnet: int}}. 0 means likely dead. Pass every candidate you are comparing in one call.",
        input_schema={
            "type": "object",
            "properties": {
                "magnets": {"type": "array", "items": {"type": "string"}},
                "magnet": {"type": "string", "description": "A single magnet; prefer `magnets`."},
            },
        },
    )
    async def probe_peers(args: dict) -> dict:
        # One call for all the candidates: the agent used to probe them one at
        # a time, a model round trip plus up to 10 s each.
        magnets = [m for m in (args.get("magnets") or []) if m]
        if args.get("magnet"):
            magnets.append(args["magnet"])
        magnets = list(dict.fromkeys(magnets))[:PROBE_MAX]
        _narrate(t.sink, STAGE_SEARCHING, "Checking who's sharing it…")
        try:
            # settle_s: the probe stops when the first magnet shows a peer, so
            # the others would read 0 when compared. It waits a few seconds
            # more for them.
            # Wait for a background probe already looking for these, rather
            # than racing it into a second probe of the same magnets.
            pending = {t.probing[m] for m in magnets if m in t.probing and not t.probing[m].done()}
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            counts = await _probe(magnets, PROBE_SETTLE_S if len(magnets) > 1 else None)
            return _out({"peers": counts})
        except Exception as e:
            return _out({"error": str(e), "peers": {}})

    @tool(
        name="say",
        description="Say one short line to the user in the chat, without ending the turn. No markdown.",
        input_schema={"text": str},
    )
    async def say(args: dict) -> dict:
        text = (args.get("text") or "").strip()
        if not text:
            return _out("ignored: empty")
        try:
            t.sink.send(text)
            return _out("sent")
        except Exception as e:
            return _out(f"error: {e}")

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
            return _out("error: no options")
        # Carried on the tool call rather than left to a preceding send_sms.
        # Observed live: the agent asked with no preamble at all, so the user
        # got a bare list of near-identical rows and no reason for the question.
        # A prompt instruction can be ignored; a required argument cannot.
        question = (args.get("question") or "").strip() or "Which one?"
        # Falls back to `edition`, the costlier reading: mislabelling a search
        # as a download is a smaller error than the reverse, where a tap the
        # user thought was cheap starts twenty minutes of work.
        kind = "book" if str(args.get("kind") or "").lower() == "book" else "edition"

        # Shown as tappable cards; the magnets stay on the server.
        await t.on_ask(question, kind, options)
        t.outcome = AgentOutcome(kind="asked", options=options)
        return _out("asked user; conversation ended")

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
            return _out({"matches": []})
        try:
            matches = await _search_library(t.settings, query)
        except Exception as e:  # noqa: BLE001
            # A library that can't be reached must not fail the turn — the
            # answer degrades to "I can't see your shelf right now", which is
            # far better than the whole request erroring.
            logger.warning("library search failed: %r", e)
            return _out({"error": "library unavailable"})
        return _out({"matches": matches})

    @tool(
        name="inspect_book",
        description=(
            "Everything the library knows about one book it owns, by the `id` "
            "from search_library: narrators, series and position, publisher, "
            "year, length, chapters, description. If no narrator is recorded, "
            "`cover_says` holds what the cover prints."
        ),
        input_schema={"item_id": str},
    )
    async def inspect_book(args: dict) -> dict:
        item_id = (args.get("item_id") or "").strip()
        if not item_id:
            return _out("error: item_id required")
        _narrate(t.sink, STAGE_SEARCHING, "Looking at your copy…")
        try:
            book = await _inspect_book(t.settings, item_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("inspect_book failed: %r", e)
            return _out({"error": "book unavailable"})
        return _out(book)

    @tool(
        name="check_downloads",
        description=(
            "The family's recent downloads, newest first, each with how it went "
            "(done, already owned, failed and why, still going), plus the books "
            "you are watching for. Use for 'is it done?', 'I don't see it in my "
            "library', 'what happened to X?'."
        ),
        input_schema={"type": "object", "properties": {}},
    )
    async def check_downloads(args: dict) -> dict:
        if t.downloads is None:
            return _out({"error": "not available"})
        try:
            data = await t.downloads()
        except Exception as e:  # noqa: BLE001
            logger.warning("check_downloads failed: %r", e)
            return _out({"error": "downloads unavailable"})
        return _out(data)

    @tool(
        name="watch_for",
        description=(
            "Remember a book that is not on the site yet. Every night new uploads "
            "are checked against the list, and when it appears it is fetched and "
            "the user hears about it here. Only after they say yes to watching."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "author": {"type": "string"},
                "note": {"type": "string", "description": "Narrator or edition they want, if any."},
            },
            "required": ["title"],
        },
    )
    async def watch_for(args: dict) -> dict:
        title = (args.get("title") or "").strip()
        if not title or t.on_watch is None:
            return _out({"error": "cannot watch"})
        try:
            wish = await t.on_watch(title, (args.get("author") or "").strip(), (args.get("note") or "").strip())
        except Exception as e:  # noqa: BLE001
            logger.warning("watch_for failed: %r", e)
            return _out({"error": "could not save"})
        return _out({"watching": wish})

    @tool(
        name="commit_download",
        description="Start the BG download. `primary` and each `fallbacks` entry: {magnet, title, author, narrator, format}. Include narrator and format on `primary` whenever the search result or analyze_cover gave them — they're shown to the user so they can see which edition was chosen. ALWAYS include 1-2 fallbacks when you have viable alternates — the polling layer uses them if the primary stalls.",
        input_schema={
            "type": "object",
            "properties": {
                "primary": {"type": "object"},
                "fallbacks": {"type": "array"},
                "force": {
                    "type": "boolean",
                    "description": "Only when they asked for another copy of a book they own.",
                },
            },
            "required": ["primary"],
        },
    )
    async def commit_download(args: dict) -> dict:
        if (ended := _already_ended()) is not None:
            return ended
        primary = args.get("primary") or {}
        fallbacks = args.get("fallbacks") or []
        if not args.get("force"):
            owned = await _owned(t.settings, primary.get("title") or "", primary.get("author") or "")
            if owned:
                return _out({
                    "refused": "already on the shelf; nothing downloaded",
                    "owned": owned,
                    "next": "Tell them they have it, with the narrator. Pass force only if they want another copy.",
                })

        magnet = primary.get("magnet")
        title = primary.get("title") or "Unknown"
        author = primary.get("author") or ""
        narrator = primary.get("narrator") or ""
        file_format = primary.get("format") or ""
        if not magnet:
            return _out("error: primary.magnet required")

        bg_title = f"{title} - {author}" if author else title
        _narrate(t.sink, STAGE_FOUND, f"Starting “{title}”…")
        try:
            download = await asyncio.to_thread(_execute_download_bg, bg_title, magnet, None)
        except Exception as e:
            logger.exception("commit_download failed to start BG process")
            return _out(f"error: {e}")

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
        t.outcome = AgentOutcome(
            kind="committed",
            download=download,
            fallbacks=clean_fallbacks,
            display=display,
            title=title,
            author=author,
            narrator=narrator,
            file_format=file_format,
        )
        return _out({"id": download.get("id"), "started": True})

    return [
        search_audiobookbay,
        analyze_cover,
        probe_peers,
        say,
        ask_user_to_pick,
        search_library,
        inspect_book,
        check_downloads,
        watch_for,
        commit_download,
    ]


def _options(tools: list) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=AGENT_MODEL,
        max_turns=AGENT_MAX_TURNS,
        system_prompt=SYSTEM_PROMPT + APP_ASK_CLAUSE,
        allowed_tools=[f"mcp__atb__{tool.name}" for tool in tools],
        # No built-in Claude Code tools (Bash, Read, …): the agent only uses its
        # own, and their definitions were in every request.
        tools=[],
        # Load our tools up front. With tool search on, every turn started with
        # a model round trip just to find them (1.5-3.5 s).
        env={"ENABLE_TOOL_SEARCH": "false"},
        # Without this the CLI's own error text is lost ("Check stderr output
        # for details"), and an auth failure looks like any other.
        stderr=lambda line: logger.warning("claude cli: %s", line.rstrip()),
    )


class AgentSession:
    """One conversation with the agent, kept open between turns.

    Starting the claude CLI costs 3-4 s, and a fresh process also forgets the
    copies it looked at last turn, so a follow-up ("the same narrator") searched
    again from a text summary. A session pays the start-up once and keeps that
    context. It is not shared: one session per conversation, one turn at a time.
    """

    def __init__(self) -> None:
        self.turns = 0
        # Set when a turn fails. The CLI may be dead, so the owner drops it.
        self.broken = False
        self.last_used = time.monotonic()
        # Kept across turns: a follow-up often re-checks the same copies.
        self.search_cache: dict[tuple[str, int, str, str], str] = {}
        self.peer_counts: dict[str, int] = {}
        # One probe at a time: each opens a libtorrent session on one port.
        self.probe_lock = asyncio.Lock()
        self.probing: dict[str, asyncio.Task] = {}
        self._new_turn("", None, None, None)
        # What the worker last told this session about the conversation: the
        # message ids it has seen and each download's last reported state. The
        # next turn is told only what changed since.
        self.seen_ids: set[str] = set()
        self.job_states: dict[str, str] = {}
        self._client: ClaudeSDKClient | None = None
        # One turn at a time. Two turns raced on one session in a live test:
        # the second found the CLI still connecting and crashed.
        self._lock = asyncio.Lock()
        self._tools = _build_tools(self)
        self._server = create_sdk_mcp_server(name="atb", tools=self._tools)

    def _new_turn(
        self, raw_query, settings, sink, on_ask, downloads=None, on_watch=None
    ) -> None:
        self.downloads = downloads
        self.on_watch = on_watch
        self.raw_query = raw_query
        self.settings = settings
        self.sink = sink
        self.on_ask = on_ask
        self.outcome: AgentOutcome | None = None
        # The round number lets repeated searches say "Searching again".
        self.search_attempts = 0

    async def turn(
        self,
        raw_query: str,
        settings: Settings,
        sink,
        *,
        on_ask: Callable[[str, str, list[dict]], Awaitable[None]],
        pending_options: list[dict] | None = None,
        history: list[tuple[str, str]] | None = None,
        downloads: Callable[[], Awaitable[dict]] | None = None,
        on_watch: Callable[[str, str, str], Awaitable[dict]] | None = None,
        event: str | None = None,
    ) -> AgentOutcome:
        """Run one request, or react to `event` (something that happened, not
        something the user said). `history` is the conversation so far on a
        fresh session, and what changed since the last turn on an open one."""
        self.last_used = time.monotonic()
        async with self._lock:
            self._new_turn(raw_query, settings, sink, on_ask, downloads, on_watch)
            prompt = _prompt(raw_query, pending_options, history, event, fresh=self.turns == 0)
            self.turns += 1
            try:
                return await self._run(prompt)
            finally:
                self.last_used = time.monotonic()
                # Drop this turn's sink and callbacks: they hold the worker's
                # context, and the session idles for up to 15 minutes.
                self._new_turn("", None, None, None)

    async def _run(self, prompt: str) -> AgentOutcome:
        # The SDK reports running out of turns as a result message, then raises
        # the same bare "exit code 1" as a dead CLI. Remembered so the two get
        # different sentences.
        ran_out = False
        # The text the agent wrote after its last tool call, if any.
        final_text = ""
        try:
            if self._client is None:
                options = _options(self._tools)
                options.mcp_servers = {"atb": self._server}
                client = ClaudeSDKClient(options=options)
                await client.connect()
                self._client = client
            await self._client.query(prompt)
            async for msg in self._client.receive_response():
                if isinstance(msg, ResultMessage):
                    ran_out = msg.subtype == "error_max_turns"
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            final_text = ""
                        elif isinstance(block, TextBlock) and block.text.strip():
                            final_text = block.text.strip()
        except Exception:
            self.broken = True
            if self.outcome is not None:
                # A tool had already settled the turn; the CLI dying afterwards
                # changes nothing the user needs to hear about.
                logger.warning("agent loop ended with an error after the outcome was set")
                return self.outcome
            if ran_out:
                return _out_of_turns()
            # Typically the claude CLI dying — an expired subscription token
            # surfaces as a bare "Command failed with exit code 1".
            logger.exception("agent loop crashed")
            return AgentOutcome(
                kind="error",
                crashed=True,
                message="Couldn't reach the book finder just now. Try again shortly.",
            )

        if self.outcome is not None:
            return self.outcome
        if ran_out:
            # The session survives this, but its context is the long search
            # that ran out, which is no help to the next turn.
            self.broken = True
            return _out_of_turns()

        # The agent's last words are its reply. A separate `reply` tool made
        # this a fallback, and the agent often skipped the tool: a good answer
        # to "What is Piranesi about?" never reached the user.
        if final_text:
            self.sink.send(final_text)
            return AgentOutcome(kind="replied", message=final_text)

        # Reached when the agent stops without committing — overwhelmingly "no
        # match found". The message is user-visible, so it reads as an
        # explanation rather than as a description of our control flow.
        return AgentOutcome(
            kind="error",
            message="Couldn't find that one — try the full title and author.",
        )

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            logger.warning("closing an agent session failed", exc_info=True)


def _out_of_turns() -> AgentOutcome:
    logger.warning("agent ran out of turns (%d)", AGENT_MAX_TURNS)
    return AgentOutcome(
        kind="error",
        crashed=True,
        message=(
            "I searched for a while but couldn't pin down the right copy. "
            "Try the exact title and author, or name the narrator."
        ),
    )


def _prompt(
    raw_query: str,
    pending_options: list[dict] | None,
    history: list[tuple[str, str]] | None,
    event: str | None = None,
    *,
    fresh: bool = True,
) -> str:
    parts: list[str] = []
    if history:
        # Oldest first, so the last line before the new request is the most
        # recent thing that happened — which is what a follow-up refers to.
        rendered = "\n".join(f"{role}: {text}" for role, text in history)
        heading = "Earlier in this conversation:" if fresh else "Since your last message:"
        parts.append(f"{heading}\n{rendered}\n")
    if event:
        parts.append(f"Something happened (this is not the user speaking): {event}")
    else:
        parts.append(f"User said: {raw_query!r}")
    if pending_options:
        formatted = "\n".join(
            f"{i+1}. {o.get('title','?')} (narrator: {o.get('narrator','?')}, author: {o.get('author','?')})"
            for i, o in enumerate(pending_options)
        )
        parts.append(f"\nPending options from earlier conversation (still valid):\n{formatted}")
    parts.append(
        "\nResolve this. Use tools as needed. End with commit_download, ask_user_to_pick, or your answer."
    )
    return "\n".join(parts)


__all__ = ["AgentOutcome", "AgentSession"]
