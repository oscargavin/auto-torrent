"""Hydrate an LLM book recommendation (title + author) into a rich BookCard.

Mirrors AudiobookShelf's own metadata pipeline so covers match the library:
Audible catalog search (free, no key) → fuzzy best-match → Audnexus enrich
(square Amazon cover, narrators, series, genres). Falls back to the Audible
search record itself if Audnexus 404s, then Open Library, then None — so a
hallucinated title that resolves to nothing is simply dropped.

All network calls are server-side and key-less. Caching is layered on top in
the recommendation builder; this module stays a pure pipeline.
"""

from __future__ import annotations

import html
import re

import requests
from rapidfuzz import fuzz

from .openlibrary import lookup_book
from .types import BookCard

# Audible region → API host TLD (matches ABS's Audible.js mapping).
REGION_TLD = {
    "us": "com", "uk": "co.uk", "ca": "ca", "au": "com.au", "fr": "fr",
    "de": "de", "jp": "co.jp", "it": "it", "in": "in", "es": "es",
}

_AMAZON_HOST = "m.media-amazon.com"
_SIZE_TOKEN = re.compile(r"\._[A-Z]{2}\d+_(?=\.[A-Za-z]+$)")
_EXT = re.compile(r"(\.[A-Za-z]+)$")
_ARTICLE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_NONWORD = re.compile(r"[^\w\s]")
_TAGS = re.compile(r"<[^>]+>")

TITLE_WEIGHT = 0.6
AUTHOR_WEIGHT = 0.4
MATCH_THRESHOLD = 70.0

_TIMEOUT = 10


def _norm(s: str) -> str:
    # Drop punctuation (don't space it out) so "J.R.R." == "JRR" and "Man's" == "Mans".
    s = _NONWORD.sub("", s.lower())
    return re.sub(r"\s{2,}", " ", s).strip()


def match_key(title: str, author: str) -> str:
    """Stable normalised 'title|author' for cache keys and matching."""
    return f"{_ARTICLE.sub('', _norm(title)).strip()}|{_norm(author)}"


def sized_cover(url: str | None, px: int = 500) -> str | None:
    """Force an Amazon cover to a square `px` longest-side; leave others as-is."""
    if not url or _AMAZON_HOST not in url:
        return url
    token = f"._SL{px}_"
    if _SIZE_TOKEN.search(url):
        return _SIZE_TOKEN.sub(token, url)
    return _EXT.sub(token + r"\1", url)


def _author_of(record: dict) -> str:
    authors = record.get("authors") or []
    return authors[0].get("name", "") if authors else ""


def _score(cand_title: str, cand_author: str, title: str, author: str) -> float:
    title_score = max(
        fuzz.token_set_ratio(_norm(title), _norm(cand_title)),
        fuzz.partial_ratio(_norm(title), _norm(cand_title)),
    )
    if not author:
        return title_score
    author_score = fuzz.token_set_ratio(_norm(author), _norm(cand_author))
    return title_score * TITLE_WEIGHT + author_score * AUTHOR_WEIGHT


def best_match(
    products: list[dict], title: str, author: str, threshold: float = MATCH_THRESHOLD
) -> dict | None:
    """Pick the closest Audible search result, or None if nothing clears `threshold`."""
    best: dict | None = None
    best_score = 0.0
    for p in products:
        score = _score(p.get("title", ""), _author_of(p), title, author)
        if score > best_score:
            best, best_score = p, score
    return best if best_score >= threshold else None


def _year(date_str: str | None) -> int | None:
    if date_str and len(date_str) >= 4 and date_str[:4].isdigit():
        return int(date_str[:4])
    return None


def parse_book(data: dict, cover_px: int = 500) -> BookCard:
    """Audnexus /books/{asin} JSON → BookCard."""
    series = data.get("seriesPrimary") or None
    # `summary` is the full blurb (HTML); `description` is a short teaser that
    # ends in "…". Prefer the full one, stripped of tags and entity-decoded.
    summary = html.unescape(_TAGS.sub("", data.get("summary") or "")).strip()
    return BookCard(
        title=data.get("title", ""),
        author=_author_of(data),
        asin=data.get("asin"),
        subtitle=data.get("subtitle") or None,
        narrators=tuple(n["name"] for n in (data.get("narrators") or []) if n.get("name")),
        description=summary or (data.get("description") or ""),
        cover_url=sized_cover(data.get("image"), cover_px),
        series=series.get("name") if series else None,
        series_position=series.get("position") if series else None,
        genres=tuple(
            g["name"] for g in (data.get("genres") or [])
            if g.get("type") == "genre" and g.get("name")
        ),
        runtime_min=data.get("runtimeLengthMin"),
        year=_year(data.get("releaseDate")),
        source="audible",
    )


def parse_audible_product(product: dict, cover_px: int = 500) -> BookCard:
    """Audible catalog-search product → BookCard (fallback when Audnexus 404s)."""
    images = product.get("product_images") or {}
    cover = None
    if images:
        largest = images[str(max(int(k) for k in images))]
        cover = sized_cover(largest, cover_px)
    series_list = product.get("series") or []
    series = series_list[0] if series_list else None
    summary = product.get("merchandising_summary") or product.get("short_description") or ""
    return BookCard(
        title=product.get("title", ""),
        author=_author_of(product),
        asin=product.get("asin"),
        subtitle=product.get("subtitle") or None,
        narrators=tuple(n["name"] for n in (product.get("narrators") or []) if n.get("name")),
        description=html.unescape(_TAGS.sub("", summary)).strip(),
        cover_url=cover,
        series=series.get("title") if series else None,
        series_position=str(series["sequence"]) if series and series.get("sequence") else None,
        runtime_min=product.get("runtime_length_min"),
        year=_year(product.get("release_date")),
        source="audible",
    )


def search_audible(
    title: str, author: str = "", region: str = "uk", limit: int = 10
) -> list[dict]:
    """Audible catalog search → list of product dicts (unauthenticated)."""
    tld = REGION_TLD.get(region, "co.uk")
    params = {
        "title": title,
        "num_results": str(limit),
        "products_sort_by": "Relevance",
        "response_groups": "contributors,product_desc,media,series",
        "image_sizes": "500,1024",
    }
    if author:
        params["author"] = author
    resp = requests.get(
        f"https://api.audible.{tld}/1.0/catalog/products", params=params, timeout=_TIMEOUT
    )
    resp.raise_for_status()
    return resp.json().get("products", [])


def fetch_audnex(asin: str, region: str = "uk") -> dict | None:
    """Audnexus book lookup. None on 404 or a REGION_UNAVAILABLE error body."""
    resp = requests.get(
        f"https://api.audnex.us/books/{asin}", params={"region": region}, timeout=_TIMEOUT
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    return None if "error" in data else data


def _openlibrary_fallback(title: str, author: str) -> BookCard | None:
    try:
        meta = lookup_book(f"{title} {author}".strip())
    except Exception:
        meta = None
    if not meta:
        return None
    cover = (
        f"https://covers.openlibrary.org/b/id/{meta.cover_id}-L.jpg"
        if meta.cover_id
        else None
    )
    return BookCard(
        title=meta.title or title,
        author=meta.author or author,
        cover_url=cover,
        series=meta.series,
        year=meta.year,
        source="openlibrary",
    )


def hydrate(title: str, author: str = "", region: str = "uk") -> BookCard | None:
    """title + author → BookCard, or None if it resolves to nothing."""
    try:
        products = search_audible(title, author, region)
    except requests.RequestException:
        products = []

    match = best_match(products, title, author) if products else None
    if match:
        asin = match.get("asin")
        data = None
        if asin:
            try:
                data = fetch_audnex(asin, region)
            except requests.RequestException:
                data = None
        return parse_book(data) if data else parse_audible_product(match)

    return _openlibrary_fallback(title, author)
