import re
import threading
import time
from pathlib import Path

import requests
from rapidfuzz import fuzz

from .server.library_match import fold
from .types import BookMetadata

_ARTICLES = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_NOISE = re.compile(
    r"\b(graphic\s*audio|unabridged|abridged|audiobook|audio\s*book|narrated\s+by\b.*"
    r"|read\s+by\b.*|full\s*cast)\b",
    re.IGNORECASE,
)
_TRAILING_DIGITS = re.compile(r"\s+\d+\s*$")


def _clean_query(query: str) -> str:
    """Strip audiobook noise words and trailing volume numbers."""
    cleaned = _NOISE.sub("", query).strip()
    cleaned = _TRAILING_DIGITS.sub("", cleaned).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned or query


def _query_variations(query: str) -> list[str]:
    """Generate query variations: cleaned, original, without articles, without subtitle."""
    cleaned = _clean_query(query)
    variations: list[str] = []
    if cleaned != query:
        variations.append(cleaned)
    variations.append(query)
    without_article = _ARTICLES.sub("", cleaned).strip()
    if without_article not in variations:
        variations.append(without_article)
    for sep in (":", " - ", " — "):
        if sep in cleaned:
            base = cleaned.split(sep)[0].strip()
            if base and base not in variations:
                variations.append(base)
            without_art = _ARTICLES.sub("", base).strip()
            if without_art and without_art not in variations:
                variations.append(without_art)
            break
    return variations


_FIELDS = "title,author_name,subject,series_name,key,first_publish_year,cover_i"
_BY = re.compile(r"\s+by\s+", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9 ]")


def _split_title_author(query: str) -> tuple[str, str]:
    """Split "The Poppy War by R.F. Kuang" into its two halves.

    Splits on the *last* " by ", so a title that contains the word ("Gone by
    Midnight by Jane Harper") keeps its own words. Returns an empty author when
    the query isn't in that shape.
    """
    matches = list(_BY.finditer(query))
    if not matches:
        return query.strip(), ""
    last = matches[-1]
    title = query[: last.start()].strip()
    author = query[last.end() :].strip()
    if not title or not author:
        return query.strip(), ""
    return title, author


# One session so repeat lookups reuse the TLS connection.
_session = requests.Session()


def _try_query(q: str = "", *, title: str = "", author: str = "") -> list[dict]:
    # 10, not 5: the single book is often outranked by editions and box sets,
    # and scoring can only choose between what it is given.
    params: dict[str, object] = {"limit": 10, "fields": _FIELDS}
    if q:
        params["q"] = q
    if title:
        params["title"] = title
    if author:
        params["author"] = author
    resp = _session.get(
        "https://openlibrary.org/search.json", params=params, timeout=10
    )
    resp.raise_for_status()
    return resp.json().get("docs", [])


def _norm(text: str) -> str:
    # Punctuation is deleted, not spaced, so "R.F." is one word as "rf".
    return _NON_ALNUM.sub("", fold(text)).strip()


def _title_score(doc_title: str, want: str) -> int:
    a, b = _norm(doc_title), _norm(want)
    if not a or not b:
        return 0
    if a == b:
        return 100
    a2, b2 = _ARTICLES.sub("", a).strip(), _ARTICLES.sub("", b).strip()
    if a2 == b2:
        return 95
    # One is a prefix of the other, scaled by how much extra the longer one
    # carries. A flat score here rated "Dune, Dune Messiah, Children of Dune"
    # nearly as highly as "Dune" — the omnibus starts with the right word and
    # then keeps going. "Dune Messiah" is a near miss; a 3-book collection is
    # not the book that was asked for.
    if a2.startswith(b2) or b2.startswith(a2):
        return int(70 * min(len(a2), len(b2)) / max(len(a2), len(b2)))
    wanted = set(b2.split())
    if not wanted:
        return 0
    return int(len(set(a2.split()) & wanted) / len(wanted) * 50)


def _author_score(doc_authors: list[str] | None, want: str) -> int:
    if not want:
        return 0
    wanted = set(_norm(want).split())
    if not wanted:
        return 0
    for name in doc_authors or []:
        tokens = set(_norm(name).split())
        if tokens == wanted:
            return 40
        # Initials differ constantly between sources — "R.F. Kuang" against
        # "R. F. Kuang" tokenises to {rf, kuang} vs {r, f, kuang}. A shared
        # surname is the reliable signal.
        if tokens & wanted:
            return 25
    return 0


_COLLECTION = re.compile(
    r"\b(trilogy|omnibus|box(ed)?\s*set|\d+\s*-?\s*book|books?\s+\d+\s*[-–]\s*\d+"
    r"|complete\s+(series|collection|novels)|collection|anthology|series\s+set)\b",
    re.IGNORECASE,
)


def _collection_penalty(doc_title: str, want_title: str) -> int:
    """Push box sets below the single book they contain.

    A collection legitimately matches the title of every book inside it, so
    scoring alone can't separate them. Downloading one costs gigabytes and
    produces a library item that isn't the book that was asked for. No penalty
    when the request itself asks for a collection.
    """
    if _COLLECTION.search(want_title):
        return 0
    return -60 if _COLLECTION.search(doc_title) else 0


def _split_title_score(doc_title: str, want: str) -> int:
    """_title_score that also tries each half of "Series: Title".

    For "Mistborn: The Final Empire" the omnibus that lists every word scored
    above "The Final Empire" itself. The part after the separator is usually
    the book, the part before it the series, so each half scores a little
    below a match on the whole.
    """
    score = _title_score(doc_title, want)
    for sep in (":", " - ", " — "):
        if sep in want:
            before, _, after = want.partition(sep)
            score = max(
                score,
                int(_title_score(doc_title, after) * 0.9),
                int(_title_score(doc_title, before) * 0.85),
            )
            break
    return score


def _pick_best(docs: list[dict], want_title: str, want_author: str) -> dict:
    """Choose the doc that best matches the request.

    OpenLibrary's relevance order put *The Dragon Republic* first for "The
    Poppy War by R.F. Kuang" — same author, same series, same subjects — and
    taking docs[0] meant the agent went off and searched for the sequel.
    Ties keep OpenLibrary's own ordering.
    """
    best, best_score = docs[0], -1
    for doc in docs:
        title = doc.get("title", "")
        score = (
            _split_title_score(title, want_title)
            + _author_score(doc.get("author_name"), want_author)
            + _collection_penalty(title, want_title)
        )
        if score > best_score:
            best, best_score = doc, score
    return best


def _strip_author(query: str, author: str) -> str | None:
    """The title left over when `author` sits at the start or end of `query`.

    "Dune Frank Herbert" has no " by ", so the whole sentence was scored as the
    title, and "Dune Messiah" outscored "Dune" on shared words. The author names
    on the returned docs tell us which words to drop. Initials may be written
    as one token ("R.F." against "R. F."). Returns None when the author is not
    at either end, or when nothing would be left.
    """
    pairs = [(raw, _norm(raw)) for raw in query.split()]
    pairs = [(raw, low) for raw, low in pairs if low]
    names = _norm(author).split()
    if not names or len(pairs) < 2:
        return None
    initials = {n[0] for n in names}

    def is_author(word: str) -> bool:
        return word in names or (len(word) <= 3 and set(word) <= initials)

    end = len(pairs)
    while end > 0 and is_author(pairs[end - 1][1]):
        end -= 1
    if 0 < end < len(pairs) and names[-1] in {low for _, low in pairs[end:]}:
        return " ".join(raw for raw, _ in pairs[:end])

    start = 0
    while start < len(pairs) and is_author(pairs[start][1]):
        start += 1
    if 0 < start < len(pairs) and names[-1] in {low for _, low in pairs[:start]}:
        return " ".join(raw for raw, _ in pairs[start:])
    return None


def _infer_author(query: str, docs: list[dict]) -> tuple[str, str] | None:
    # A doc titled with the whole query means the query is a title, even when
    # some doc has an author that overlaps it ("A Game of Thrones" has a doc
    # by "Book Of Thrones").
    if any(_title_score(doc.get("title", ""), query) >= 95 for doc in docs):
        return None
    for doc in docs:
        for name in doc.get("author_name") or []:
            title = _strip_author(query, name)
            # The rest must name a book by that author, or the author match
            # is a coincidence.
            if title and _words(title) <= _words(doc.get("title", "")):
                return title, name
    return None


def _words(text: str) -> set[str]:
    return set(_ARTICLES.sub("", _norm(text)).split())


def _confident(doc_title: str, want_title: str) -> bool:
    """Whether the chosen doc is plausibly the book that was asked for.

    A wrong title is worse than none: the agent then searches for a book the
    user never named. Callers fall back to the raw query on None.
    """
    a = _ARTICLES.sub("", _norm(doc_title)).strip()
    b = _ARTICLES.sub("", _norm(want_title)).strip()
    if not a or not b:
        return False
    if fuzz.ratio(a, b) >= 80:
        return True
    have, want = set(a.split()), set(b.split())
    # The doc title is inside the query (a series prefix or subtitle was
    # typed too), or the query is the doc title short of one word ("Hail
    # Mary"). "The Worlds of Frank Herbert" for "Frank Herbert" is neither.
    return have <= want or (want <= have and len(have - want) <= 1)


def _series(doc: dict) -> str | None:
    # Only real series data. Subjects are genres and places ("American
    # Science fiction", "Dune (Imaginary place)"), and a genre searched as a
    # series fills the fan-out with unrelated books.
    names = doc.get("series_name") or []
    if names:
        return names[0]
    for subj in doc.get("subject") or []:
        if subj.startswith("franchise:"):
            return subj.split(":", 1)[1].strip()
    return None


def _lookup(query: str) -> BookMetadata | None:
    want_title, want_author = _split_title_author(query)

    docs: list[dict] = []
    # A structured title+author search is far more precise than throwing the
    # whole sentence at `q`, which ranks anything by the same author highly.
    if want_author:
        try:
            docs = _try_query(title=_clean_query(want_title), author=want_author)
        except requests.RequestException:
            docs = []

    if not docs:
        for variation in _query_variations(query):
            docs = _try_query(variation)
            if docs:
                break
    if not docs:
        return None

    inferred = None if want_author else _infer_author(_clean_query(want_title), docs)
    if inferred:
        want_title, want_author = inferred

    doc = _pick_best(docs, want_title, want_author)

    # The plain query can miss the single book entirely ("The Poppy War R.F.
    # Kuang" returns only the omnibus), so ask again in the structured form.
    if inferred and _title_score(doc.get("title", ""), want_title) < 95:
        try:
            extra = _try_query(title=want_title, author=want_author)
        except requests.RequestException:
            extra = []
        if extra:
            doc = _pick_best(extra + docs, want_title, want_author)

    if not _confident(doc.get("title", ""), want_title):
        return None

    return BookMetadata(
        title=doc.get("title", ""),
        author=(doc.get("author_name") or [""])[0],
        year=doc.get("first_publish_year"),
        cover_id=doc.get("cover_i"),
        series=_series(doc),
    )


_CACHE_TTL = 600
_CACHE_MAX = 256
_cache: dict[str, tuple[float, BookMetadata | None]] = {}
_cache_lock = threading.Lock()


def lookup_book(query: str) -> BookMetadata | None:
    # The agent often looks up the same query again within one chat, and a
    # repeat round costs a full OpenLibrary call each time. Failures raise and
    # are never cached.
    key = " ".join(query.lower().split())
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]

    book = _lookup(query)
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (now, book)
    return book


def download_cover(cover_id: int, dest: Path) -> Path | None:
    cover_path = dest / "cover.jpg"
    if cover_path.exists():
        return cover_path
    url = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200 and len(resp.content) > 1000:
            cover_path.write_bytes(resp.content)
            return cover_path
    except requests.RequestException:
        pass
    return None
