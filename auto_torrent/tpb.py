"""Search The Pirate Bay via apibay.org JSON API."""

import math
import re
from dataclasses import dataclass, replace
from typing import NamedTuple
from urllib.parse import quote

import requests

from .config import (
    DEFAULT_TRACKERS,
    TPB_CODEC_SCORES,
    TPB_MAX_SEED_SCORE,
    TPB_RESOLUTION_DISTANCE_SCORES,
    TPB_RESOLUTION_LADDER,
    TPB_SEED_LOG_SCALE,
    TPB_SOURCE_SCORES,
    TPB_STATUS_SCORES,
)

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

_SIZE_WARNINGS: list[tuple[str, int]] = [
    ("2160p", 2 * 1024**3),
    ("4k", 2 * 1024**3),
    ("uhd", 2 * 1024**3),
    ("1080p", 500 * 1024**2),
    ("720p", 200 * 1024**2),
]


class TPBError(Exception):
    pass


@dataclass(frozen=True)
class TPBResult:
    title: str
    link: str
    magnet: str
    file_size: str
    size_bytes: int
    seeders: int
    category: str
    status: str = "member"
    score: int = 0
    warning: str | None = None


def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1024**3:
        return f"{size_bytes / 1024**3:.1f} GB"
    if size_bytes >= 1024**2:
        return f"{size_bytes / 1024**2:.0f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes} B"


def _build_magnet(info_hash: str, name: str) -> str:
    tracker_params = "&".join(f"tr={quote(t)}" for t in DEFAULT_TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash}&dn={quote(name)}&{tracker_params}"


def _check_size_warning(title: str, size_bytes: int) -> str | None:
    title_lower = title.lower()
    for keyword, min_size in _SIZE_WARNINGS:
        if keyword in title_lower and size_bytes < min_size:
            return f"Claims {keyword} but only {_format_size(size_bytes)} — may be fake"
    return None


_RESOLUTION_RE = re.compile(r"\b(2160p|1080p|720p|480p)\b", re.IGNORECASE)
_CODEC_RE = re.compile(r"\b(x265|h\.?265|hevc|x264|h\.?264|avc|av1)\b", re.IGNORECASE)
_HDR_RE = re.compile(r"\b(HDR10\+?|HDR|DV|Dolby\.?Vision|HLG)\b", re.IGNORECASE)

_SOURCE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("bluray", re.compile(r"\b(blu-?ray|bdremux|bdrip|brrip)\b", re.IGNORECASE)),
    ("web-dl", re.compile(r"\b(web-?dl|webdl)\b", re.IGNORECASE)),
    ("webrip", re.compile(r"\bwebrip\b", re.IGNORECASE)),
    ("hdtv", re.compile(r"\b(hdtv|pdtv)\b", re.IGNORECASE)),
    ("hdrip", re.compile(r"\bhdrip\b", re.IGNORECASE)),
    ("cam", re.compile(r"\b(cam|ts|hdts|telesync|tc|hdtc|screener|scr)\b", re.IGNORECASE)),
]

class TitleInfo(NamedTuple):
    resolution: str | None
    source: str | None
    codec: str | None
    hdr: bool


def _resolution_scores(prefer: str = "1080p") -> dict[str, int]:
    """Score resolutions by distance from preferred. Preferred=20, ±1 step=12, ±2=5, ±3=2."""
    prefer_idx = TPB_RESOLUTION_LADDER.index(prefer) if prefer in TPB_RESOLUTION_LADDER else 2
    scores: dict[str, int] = {}
    for i, res in enumerate(TPB_RESOLUTION_LADDER):
        dist = abs(i - prefer_idx)
        scores[res] = TPB_RESOLUTION_DISTANCE_SCORES.get(dist, 2)
    return scores


def parse_title(title: str) -> TitleInfo:
    res_match = _RESOLUTION_RE.search(title)
    resolution = res_match.group(1).lower() if res_match else None

    source = None
    for name, pattern in _SOURCE_PATTERNS:
        if pattern.search(title):
            source = name
            break

    codec_match = _CODEC_RE.search(title)
    codec = None
    if codec_match:
        raw = codec_match.group(1).lower().replace(".", "")
        if raw in ("x265", "h265", "hevc"):
            codec = "x265"
        elif raw in ("x264", "h264", "avc"):
            codec = "x264"
        elif raw == "av1":
            codec = "av1"

    hdr = bool(_HDR_RE.search(title))

    return TitleInfo(resolution=resolution, source=source, codec=codec, hdr=hdr)


def score_result(r: TPBResult, prefer_resolution: str = "1080p") -> int:
    info = parse_title(r.title)
    res_scores = _resolution_scores(prefer_resolution)

    seed_score = min(TPB_MAX_SEED_SCORE, int(math.log2(r.seeders + 1) * TPB_SEED_LOG_SCALE)) if r.seeders > 0 else 0
    source_score = TPB_SOURCE_SCORES.get(info.source or "", 10)
    res_score = res_scores.get(info.resolution or "", 8)
    trust_score = TPB_STATUS_SCORES.get(r.status, 3)
    codec_score = TPB_CODEC_SCORES.get(info.codec or "", 0)
    hdr_score = 5 if info.hdr else 0

    total = seed_score + source_score + res_score + trust_score + codec_score + hdr_score
    return min(100, total)


def search(
    query: str,
    category: str = "video",
    min_seeds: int = 5,
    quality: str = "1080p",
    proxy: str | None = None,
) -> list[TPBResult]:
    allowed_cats: set[str] | None = None
    if category != "all":
        allowed_cats = CATEGORY_GROUPS.get(category)

    url = f"{APIBAY_URL}/q.php?q={quote(query)}"
    kwargs: dict = {"timeout": 15}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    try:
        resp = requests.get(url, **kwargs)
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

    results: list[TPBResult] = []
    for item in data:
        try:
            cat_id = str(item.get("category", ""))
            seeders = int(item.get("seeders", "0"))
            size_bytes = int(item.get("size", 0))
        except (ValueError, TypeError):
            continue

        if allowed_cats and cat_id not in allowed_cats:
            continue
        if seeders < min_seeds:
            continue

        name = item.get("name", "")

        result = TPBResult(
            title=name,
            link=f"https://thepiratebay.org/description.php?id={item.get('id', '')}",
            magnet=_build_magnet(item.get("info_hash", ""), name),
            file_size=_format_size(size_bytes),
            size_bytes=size_bytes,
            seeders=seeders,
            category=CATEGORIES.get(cat_id, cat_id),
            status=item.get("status", "member"),
            warning=_check_size_warning(name, size_bytes),
        )
        results.append(replace(result, score=score_result(result, quality)))

    results.sort(key=lambda r: r.score, reverse=True)
    return results
