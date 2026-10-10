"""Books a profile asked us to watch for, checked after each index refresh.

Layout:
  - HSET wishlist:{profile_id}   — wish id → JSON wish
  - SADD wishlist:profiles       — every profile that has a wish, so the
                                   nightly check needs no profile store

A wish stays until a new post matches it or the user removes it. The TTL only
clears a list nobody has touched for half a year.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING

from redis.asyncio import Redis

from ..types import SearchResult

if TYPE_CHECKING:
    from .threads.store import ThreadStore

logger = logging.getLogger("atb.wishlist")

WISH_TTL_S = 180 * 24 * 3600
_PROFILES_KEY = "wishlist:profiles"

# Words that carry no meaning in a title, so "The Late Lord Thorpe" and
# "Late Lord Thorpe" are the same wish.
_STOPWORDS = frozenset({
    "a", "an", "the", "of", "and", "to", "in", "on", "for", "at", "by", "with", "from", "or",
})


def _key(profile_id: str) -> str:
    return f"wishlist:{profile_id}"


async def add(redis: Redis, profile_id: str, title: str, author: str = "", note: str = "") -> dict:
    wish = {
        "id": uuid.uuid4().hex,
        "title": title.strip(),
        "author": author.strip(),
        "note": note.strip(),
        "created_at": time.time(),
    }
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(_key(profile_id), wish["id"], json.dumps(wish))
        pipe.expire(_key(profile_id), WISH_TTL_S)
        pipe.sadd(_PROFILES_KEY, profile_id)
        await pipe.execute()
    return wish


async def list_for(redis: Redis, profile_id: str) -> list[dict]:
    raw = await redis.hvals(_key(profile_id))
    return sorted((json.loads(v) for v in raw), key=lambda w: w["created_at"])


async def remove(redis: Redis, profile_id: str, wish_id: str) -> bool:
    return bool(await redis.hdel(_key(profile_id), wish_id))


# ---- matching ----


def _words(text: str) -> list[str]:
    """Lower-case words with accents, apostrophes, and punctuation removed, so
    "Fool’s Errand" and "fools errand" give the same words."""
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"['‘’`´]", "", text)
    return re.findall(r"[^\W_]+", text)


def _significant(words: Iterable[str]) -> set[str]:
    words = list(words)
    return {w for w in words if w not in _STOPWORDS} or set(words)


def _split_post(post_title: str) -> tuple[str, str, str]:
    """(main title, everything but the author, author) of an ABB post title.

    ABB titles read "Title (Series #1) - Author", sometimes with a series part
    between ("Title - DC Smith series bk11 - Author"), and the author is often
    cut short ("... - Robin").
    """
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", post_title)]
    if len(parts) > 1:
        body, author = " ".join(parts[:-1]), parts[-1]
    else:
        body, author = parts[0], ""
    # The main title stops at the first series or edition note, or subtitle.
    main = re.split(r"[(\[:]", parts[0], maxsplit=1)[0]
    return main, body, author


def matches(wish: dict, post_title: str) -> bool:
    """True when the post is the book the wish names, and not a different book
    that shares a word with it ("Some sort of justice" is not "Ancillary
    Justice", and "Dune" is not "Dune Messiah").

    Every significant word of the wish must be in the post title, and every
    significant word of the post's main title must be in the wish. When the
    wish has an author and the post names one, the surname must agree.
    """
    wanted = _significant(_words(wish.get("title", "")))
    if not wanted:
        return False

    main, body, post_author = _split_post(post_title)
    if not wanted <= set(_words(body)):
        return False
    if not _significant(_words(main)) <= wanted:
        return False

    wish_author = _words(wish.get("author", ""))
    author_words = _words(post_author)
    if wish_author and author_words:
        surname = wish_author[-1]
        # A one-word author is ABB cutting the name short, which usually
        # leaves only the first name ("Fool's Errand - Robin").
        truncated = len(author_words) == 1 and author_words[0] in wish_author
        if surname not in author_words and not truncated:
            return False
    return True


# ---- the nightly check ----

EnqueueTurn = Callable[[str, str, "list[dict] | None"], Awaitable[None]]


def _turn_text(wish: dict, post: SearchResult) -> str:
    by = f" by {wish['author']}" if wish.get("author") else ""
    return (
        f"(watchlist) {wish['title']}{by} just appeared on the site as \"{post.title}\". "
        "Fetch it as if they had asked for it."
    )


async def check_new_posts(
    redis: Redis,
    threads: ThreadStore,
    enqueue_turn: EnqueueTurn,
    posts: list[SearchResult],
) -> int:
    """Start a chat turn for every wish that one of the new posts matches.

    Returns how many turns it started. A wish whose profile has no thread, or
    whose thread is busy, stays for the next refresh. One failing wish never
    stops the others.
    """
    from .threads.types import Message, MessageKind, ThreadStatus

    if not posts:
        return 0

    started = 0
    for profile_id in sorted(await redis.smembers(_PROFILES_KEY)):
        try:
            wishes = await list_for(redis, profile_id)
            if not wishes:
                await redis.srem(_PROFILES_KEY, profile_id)
                continue

            for wish in wishes:
                post = next((p for p in posts if matches(wish, p.title)), None)
                if post is None:
                    continue

                recent = await threads.list_for_profile(profile_id, limit=1)
                if not recent:
                    logger.info("wish %s matched %r but profile %s has no thread", wish["id"], post.title, profile_id)
                    continue
                thread = recent[0]
                if thread.status is ThreadStatus.working:
                    logger.info("wish %s matched %r but thread %s is busy", wish["id"], post.title, thread.id)
                    continue

                # The same steps as post_message: a new turn supersedes an
                # unanswered question, and the thread is working from now so
                # the user cannot start a second turn over this one.
                if thread.status is ThreadStatus.awaiting_choice:
                    await threads.clear_pending(thread.id)
                await threads.append(
                    thread.id,
                    Message.new(
                        thread.id,
                        MessageKind.assistant,
                        text=f"The book you asked me to watch for is on the site now: {post.title}. Getting it.",
                    ),
                )
                await threads.set_status(thread.id, ThreadStatus.working)
                await enqueue_turn(thread.id, _turn_text(wish, post), None)
                await remove(redis, profile_id, wish["id"])
                started += 1
                logger.info("wish %s matched %r: started a turn on thread %s", wish["id"], post.title, thread.id)
        except Exception:
            logger.exception("wishlist check failed for profile %s", profile_id)
    return started


async def check_after_refresh(posts: list[SearchResult]) -> int:
    """check_new_posts with the server's own Redis and arq queue, for the
    nightly refresh, which runs outside the web app."""
    from arq import create_pool
    from arq.connections import RedisSettings

    from .jobs.events import EventLog
    from .settings import Settings
    from .threads.store import ThreadStore

    settings = Settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        threads = ThreadStore(
            redis, EventLog(redis, prefix="thread"), state_ttl_s=settings.job_state_ttl_s
        )

        async def enqueue_turn(thread_id: str, text: str, pending: list[dict] | None) -> None:
            await pool.enqueue_job("run_thread_turn", thread_id, text, pending)

        return await check_new_posts(redis, threads, enqueue_turn, posts)
    finally:
        await pool.close()
        await redis.aclose()
