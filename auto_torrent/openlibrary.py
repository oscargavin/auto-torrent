import re
from pathlib import Path

import requests

from .config import GENERIC_SUBJECTS
from .types import BookMetadata

_ARTICLES = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)


def _query_variations(query: str) -> list[str]:
    """Generate query variations: original, without articles, without subtitle."""
    variations = [query]
    without_article = _ARTICLES.sub("", query).strip()
    if without_article != query:
        variations.append(without_article)
    for sep in (":", " - ", " — "):
        if sep in query:
            base = query.split(sep)[0].strip()
            if base and base not in variations:
                variations.append(base)
            without_art = _ARTICLES.sub("", base).strip()
            if without_art and without_art not in variations:
                variations.append(without_art)
            break
    return variations


def _try_query(q: str) -> list[dict]:
    resp = requests.get(
        "https://openlibrary.org/search.json",
        params={
            "q": q,
            "limit": 5,
            "fields": "title,author_name,subject,key,first_publish_year,cover_i",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("docs", [])


def lookup_book(query: str) -> BookMetadata | None:
    for variation in _query_variations(query):
        docs = _try_query(variation)
        if docs:
            break
    else:
        return None

    doc = docs[0]

    series = None
    for subj in doc.get("subject") or []:
        if subj.lower() not in GENERIC_SUBJECTS and not subj.startswith("nyt:"):
            series = subj
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
