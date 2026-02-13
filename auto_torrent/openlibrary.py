from pathlib import Path

import requests

from .config import GENERIC_SUBJECTS
from .types import BookMetadata


def lookup_book(query: str) -> BookMetadata | None:
    resp = requests.get(
        "https://openlibrary.org/search.json",
        params={
            "q": query,
            "limit": 5,
            "fields": "title,author_name,subject,key,first_publish_year,cover_i",
        },
        timeout=10,
    )
    resp.raise_for_status()
    docs = resp.json().get("docs", [])
    if not docs:
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
