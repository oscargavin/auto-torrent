"""Local mirror of AudiobookBay's listing pages.

Their `?s=` search returns an empty result set for every query (the site is up,
only its search index is broken), so we keep our own. Every listing page —
homepage and the category archives — is crawlable and parses with the same
selectors as the search page, and each archive reaches ~500 pages back, which is
far deeper into the catalogue than the homepage feed alone.
"""

import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from .abb import ABBError, fetch_html, fetch_listing, parse_listing
from .config import CONFIG_DIR
from .types import SearchResult

INDEX_PATH = CONFIG_DIR / "abb-index.sqlite3"

# Beyond its last real page every archive re-serves one fixed page forever
# rather than 404ing, so the crawler stops on a repeat rather than on an error.
MAX_PAGES = 500
WORKERS = 5

# A page can come back empty from a transient error rather than the end of the
# archive, so give an archive a couple of chances before abandoning it.
EMPTY_PAGE_TOLERANCE = 3

# Fallback for when the homepage nav can't be read; discover_categories() is
# the source of truth so a renamed or added category isn't silently missed.
KNOWN_CATEGORIES = [
    "action", "adults", "adventure", "anthology", "art",
    "autobiography-biographies", "bestsellers", "business", "children",
    "classic", "computer", "contemporary", "crime", "detective",
    "doctor-who-sci-fi", "documentary", "education", "fantasy", "full-cast",
    "general-fiction", "general-non-fiction", "historical-fiction", "history",
    "horror", "humor", "lecture", "lgbt", "libertarian", "light-novel",
    "literature", "litrpg", "military", "mystery", "new", "novel", "other",
    "paranormal", "plays-theater", "poetry", "political", "postapocalyptic",
    "radio-productions", "romance", "sci-fi", "science", "self-help",
    "short-story", "spiritual", "sports", "suspense", "teen-young-adult",
    "thriller", "true-crime", "tutorial", "westerns", "zombies",
]

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS posts (
    link TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    format TEXT DEFAULT '',
    bitrate TEXT DEFAULT '',
    file_size TEXT DEFAULT '',
    posted TEXT DEFAULT '',
    seen_at REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts
    USING fts5(title, link UNINDEXED, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS crawl_log (
    path TEXT PRIMARY KEY,
    pages INTEGER NOT NULL,
    finished_at REAL NOT NULL
);
"""

_conn: sqlite3.Connection | None = None


def connect(path: Path | None = None) -> sqlite3.Connection:
    """The shared index connection (mirrors abb._get_session). A path opens a
    separate one, which is what the tests use."""
    global _conn
    if path is not None:
        return _open(path)
    if _conn is None:
        _conn = _open(INDEX_PATH)
    return _conn


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


@contextmanager
def _index(conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    yield conn or connect()


def upsert(conn: sqlite3.Connection, results: Iterable[SearchResult], commit: bool = True) -> int:
    """Store results, returning how many were new to the index."""
    rows = list(results)
    if not rows:
        return 0

    now = time.time()
    conn.executemany(
        "INSERT INTO posts (link, title, format, bitrate, file_size, posted, seen_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(link) DO UPDATE SET seen_at=excluded.seen_at",
        [(r.link, r.title, r.format, r.bitrate, r.file_size, r.posted, now) for r in rows],
    )

    links = [r.link for r in rows]
    placeholders = ",".join("?" * len(links))
    known = {
        row[0] for row in
        conn.execute(f"SELECT link FROM posts_fts WHERE link IN ({placeholders})", links)
    }
    fresh = [(r.title, r.link) for r in rows if r.link not in known]
    conn.executemany("INSERT INTO posts_fts (title, link) VALUES (?, ?)", fresh)

    if commit:
        conn.commit()
    return len(fresh)


def last_page(html: str) -> int:
    """Every archive advertises its own last page in the pagination links, which
    is the only trustworthy end marker: past the end the site re-serves one fixed
    page forever instead of 404ing, and it also repeats the odd page mid-archive,
    so neither an error nor a repeat means "finished"."""
    numbers = [int(n) for n in re.findall(r"/page/(\d+)/", html)]
    return min(max(numbers, default=1), MAX_PAGES)


def crawl_archive(
    conn: sqlite3.Connection,
    path: str,
    max_pages: int = MAX_PAGES,
    stop_when_known: bool = False,
    workers: int = WORKERS,
) -> tuple[int, int]:
    """Walk one archive (e.g. "" or "/audio-books/type/classic").

    Returns (pages crawled, new posts). With stop_when_known it stops at the
    first page holding nothing new, which is what makes the nightly refresh cheap.
    """
    try:
        html = fetch_html(f"{path}/page/2/")
    except ABBError:
        return 0, 0

    results = parse_listing(html)
    if not results:
        return 0, 0

    pages = 1
    new_total = upsert(conn, results, commit=False)
    end = min(last_page(html), max_pages)
    empties = 0

    if not (stop_when_known and new_total == 0):
        with ThreadPoolExecutor(workers) as pool:
            for start in range(3, end + 1, workers):
                batch = range(start, min(start + workers, end + 1))
                fetched = list(pool.map(lambda p: _safe_fetch(f"{path}/page/{p}/"), batch))
                done = False

                for page_results in fetched:
                    if not page_results:
                        empties += 1
                        if empties >= EMPTY_PAGE_TOLERANCE:
                            done = True
                            break
                        continue
                    empties = 0

                    new = upsert(conn, page_results, commit=False)
                    pages += 1
                    new_total += new
                    if stop_when_known and new == 0:
                        done = True
                        break

                conn.commit()
                if done:
                    break

    conn.commit()
    return pages, new_total


def _safe_fetch(path: str) -> list[SearchResult]:
    try:
        return fetch_listing(path)
    except ABBError:
        return []


def discover_categories() -> list[str]:
    """Read the category list off the homepage nav, falling back to the last
    list we knew about."""
    try:
        found = sorted(set(re.findall(r"/audio-books/type/([a-z0-9-]+)/", fetch_html("/"))))
    except ABBError:
        found = []
    return found or KNOWN_CATEGORIES


def backfill(
    conn: sqlite3.Connection | None = None,
    on_archive: Callable[[str, int, int], None] | None = None,
) -> int:
    """Crawl every category archive. Slow (~20 min) and meant to run once."""
    with _index(conn) as db:
        total = 0
        for cat in discover_categories():
            path = f"/audio-books/type/{cat}"
            pages, new = crawl_archive(db, path)
            db.execute(
                "INSERT INTO crawl_log (path, pages, finished_at) VALUES (?, ?, ?)"
                " ON CONFLICT(path) DO UPDATE SET pages=excluded.pages, finished_at=excluded.finished_at",
                (path, pages, time.time()),
            )
            db.commit()
            total += new
            if on_archive:
                on_archive(cat, pages, new)
        return total


def refresh(conn: sqlite3.Connection | None = None) -> int:
    """Pick up posts added since the last run. Seconds, not hours."""
    with _index(conn) as db:
        _, new = crawl_archive(db, "", max_pages=40, stop_when_known=True)
        return new


def count(conn: sqlite3.Connection | None = None) -> int:
    with _index(conn) as db:
        return db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]


def _fts_query(query: str) -> str:
    """Every word must appear, in any order — "frederica heyer" finds
    "Frederica - Georgette Heyer"."""
    words = [w for w in re.split(r"[^\w']+", query.lower()) if len(w) > 1]
    return " AND ".join(f'"{w}"*' for w in words)


def search(query: str, limit: int = 20, conn: sqlite3.Connection | None = None) -> list[SearchResult]:
    match = _fts_query(query)
    if not match:
        return []
    with _index(conn) as db:
        rows = db.execute(
            "SELECT p.* FROM posts_fts f JOIN posts p ON p.link = f.link"
            " WHERE posts_fts MATCH ? ORDER BY bm25(posts_fts) LIMIT ?",
            (match, limit),
        ).fetchall()
    return [
        SearchResult(
            title=r["title"],
            link=r["link"],
            format=r["format"] or "",
            bitrate=r["bitrate"] or "",
            file_size=r["file_size"] or "",
            posted=r["posted"] or "",
        )
        for r in rows
    ]
