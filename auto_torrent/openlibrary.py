import re
from pathlib import Path

import requests

from .config import GENERIC_SUBJECTS
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


_FIELDS = "title,author_name,subject,key,first_publish_year,cover_i"
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


def _try_query(q: str = "", *, title: str = "", author: str = "") -> list[dict]:
    params: dict[str, object] = {"limit": 5, "fields": _FIELDS}
    if q:
        params["q"] = q
    if title:
        params["title"] = title
    if author:
        params["author"] = author
    resp = requests.get(
        "https://openlibrary.org/search.json", params=params, timeout=10
    )
    resp.raise_for_status()
    return resp.json().get("docs", [])


def _norm(text: str) -> str:
    return _NON_ALNUM.sub("", text.lower()).strip()


def _title_score(doc_title: str, want: str) -> int:
    a, b = _norm(doc_title), _norm(want)
    if not a or not b:
        return 0
    if a == b:
        return 100
    a2, b2 = _ARTICLES.sub("", a).strip(), _ARTICLES.sub("", b).strip()
    if a2 == b2:
        return 95
    # One contains the other: "Dune" vs "Dune Messiah" is a weaker match than
    # equality but far better than sharing a couple of words.
    if a2.startswith(b2) or b2.startswith(a2):
        return 70
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


def _pick_best(docs: list[dict], want_title: str, want_author: str) -> dict:
    """Choose the doc that best matches the request.

    OpenLibrary's relevance order put *The Dragon Republic* first for "The
    Poppy War by R.F. Kuang" — same author, same series, same subjects — and
    taking docs[0] meant the agent went off and searched for the sequel.
    Ties keep OpenLibrary's own ordering.
    """
    best, best_score = docs[0], -1
    for doc in docs:
        score = _title_score(doc.get("title", ""), want_title) + _author_score(
            doc.get("author_name"), want_author
        )
        if score > best_score:
            best, best_score = doc, score
    return best


def lookup_book(query: str) -> BookMetadata | None:
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

    doc = _pick_best(docs, want_title, want_author)

    series = None
    for subj in doc.get("subject") or []:
        low = subj.lower()
        if low not in GENERIC_SUBJECTS and not subj.startswith("nyt:") and not subj.startswith("franchise:"):
            series = subj
            break
        if subj.startswith("franchise:"):
            series = subj.split(":", 1)[1].strip()
            break

    return BookMetadata(
        title=doc.get("title", ""),
        author=(doc.get("author_name") or [""])[0],
        year=doc.get("first_publish_year"),
        cover_id=doc.get("cover_i"),
        series=series,
    )


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
