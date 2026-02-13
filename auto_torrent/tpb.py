"""Search The Pirate Bay via apibay.org JSON API."""

from urllib.parse import quote

import requests

from .config import DEFAULT_TRACKERS
from .types import SearchResult

APIBAY_URL = "https://apibay.org"

CATEGORIES = {
    "100": "Audio",
    "101": "Music",
    "102": "Audio Books",
    "103": "Sound Clips",
    "104": "FLAC",
    "199": "Audio Other",
    "200": "Video",
    "201": "Movies",
    "202": "Movies DVDR",
    "203": "Music Videos",
    "204": "Movie Clips",
    "205": "TV Shows",
    "206": "Handheld",
    "207": "HD Movies",
    "208": "HD TV Shows",
    "209": "3D",
    "210": "CAM/TS",
    "211": "UHD Movies",
    "212": "UHD TV Shows",
    "299": "Video Other",
    "300": "Applications",
    "400": "Games",
    "500": "Porn",
    "600": "Other",
}

CATEGORY_GROUPS: dict[str, set[str]] = {
    "video": {"200", "201", "202", "203", "204", "205", "206", "207", "208", "209", "210", "211", "212", "299"},
    "audio": {"100", "101", "102", "103", "104", "199"},
    "apps": {"300"},
    "games": {"400"},
}

SUSPICIOUS_EXTENSIONS = {
    ".exe", ".bat", ".scr", ".msi", ".cmd", ".ps1",
    ".vbs", ".js", ".wsf", ".com", ".pif", ".reg",
}

SIZE_WARNINGS: list[tuple[str, int]] = [
    ("2160p", 2 * 1024**3),
    ("4k", 2 * 1024**3),
    ("uhd", 2 * 1024**3),
    ("1080p", 500 * 1024**2),
    ("720p", 200 * 1024**2),
]


class TPBError(Exception):
    pass


def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / 1024 ** 3:.1f} GB"
    if size_bytes >= 1024 ** 2:
        return f"{size_bytes / 1024 ** 2:.0f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes} B"


def _build_magnet(info_hash: str, name: str) -> str:
    tracker_params = "&".join(f"tr={quote(t)}" for t in DEFAULT_TRACKERS)
    dn = quote(name)
    return f"magnet:?xt=urn:btih:{info_hash}&dn={dn}&{tracker_params}"


def check_size_warning(title: str, size_bytes: int) -> str | None:
    title_lower = title.lower()
    for keyword, min_size in SIZE_WARNINGS:
        if keyword in title_lower and size_bytes < min_size:
            return f"Claims {keyword} but only {_format_size(size_bytes)} — may be fake"
    return None


def search(
    query: str,
    category: str | None = "video",
    min_seeds: int = 5,
) -> list[SearchResult]:
    allowed_cats: set[str] | None = None
    if category and category != "all":
        allowed_cats = CATEGORY_GROUPS.get(category)

    url = f"{APIBAY_URL}/q.php?q={quote(query)}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except requests.ConnectTimeout:
        raise TPBError("The Pirate Bay is not responding (connection timed out)")
    except requests.ConnectionError:
        raise TPBError("The Pirate Bay is unreachable (connection failed)")
    except requests.HTTPError as e:
        raise TPBError(f"The Pirate Bay returned an error (HTTP {e.response.status_code})")

    data = resp.json()

    if not data or (len(data) == 1 and str(data[0].get("id")) == "0"):
        return []

    results: list[SearchResult] = []
    for item in data:
        cat_id = str(item.get("category", ""))
        seeders = int(item.get("seeders", "0"))

        if allowed_cats and cat_id not in allowed_cats:
            continue
        if seeders < min_seeds:
            continue

        info_hash = item.get("info_hash", "")
        name = item.get("name", "")
        size_bytes = int(item.get("size", 0))
        torrent_id = item.get("id", "")

        results.append(SearchResult(
            title=name,
            link=f"https://thepiratebay.org/description.php?id={torrent_id}",
            magnet=_build_magnet(info_hash, name),
            file_size=_format_size(size_bytes),
            posted=f"{seeders} seeds",
            category=CATEGORIES.get(cat_id, cat_id),
        ))

    return results
