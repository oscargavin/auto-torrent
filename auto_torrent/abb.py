import base64
import binascii
import random
import re
import time
from dataclasses import replace
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from .config import ABB_BASE_URL, CACHE_DIR, DEFAULT_TRACKERS, HEADERS
from .types import SearchResult

_REQUEST_DELAY = (1.5, 3.0)
_REQUEST_TIMEOUT = 45


class ABBError(Exception):
    pass


def _build_session() -> requests.Session:
    try:
        import requests_cache

        session = requests_cache.CachedSession(
            str(CACHE_DIR / "abb"),
            backend="sqlite",
            expire_after=3600,
        )
    except ImportError:
        session = requests.Session()

    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=10,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session


_session: requests.Session | None = None
_proxy: str | None = None


def configure(proxy: str | None = None) -> None:
    global _proxy, _session
    _proxy = proxy
    _session = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = _build_session()
        if _proxy:
            _session.proxies = {"http": _proxy, "https": _proxy}
    return _session


def _delay() -> None:
    time.sleep(random.uniform(*_REQUEST_DELAY))


def _decoded(post: Tag) -> Tag:
    """Some listing pages ship their posts base64'd into a hidden div for the
    page's own JS to expand (`<div class="post re-ab" style="display:none">`).
    Decode those so the markup looks the same either way."""
    if post.select_one(".postTitle"):
        return post
    try:
        html = base64.b64decode(post.get_text(strip=True), validate=True).decode("utf-8", "ignore")
    except (ValueError, binascii.Error):
        return post
    return BeautifulSoup(html, "html.parser")


def parse_listing(html: str) -> list[SearchResult]:
    """Parse any AudiobookBay listing page (search, homepage, category, tag)."""
    soup = BeautifulSoup(html, "html.parser")
    results: list[SearchResult] = []

    for post in soup.select(".post"):
        post = _decoded(post)
        title_el = post.select_one(".postTitle > h2 > a")
        if not title_el:
            continue

        fields: dict[str, str] = {}
        details_p = post.select_one(".postContent p[style*='text-align:center']")
        if details_p:
            details = str(details_p)
            for field in ("Format", "Bitrate", "File Size"):
                m = re.search(rf"{field}:\s*<span[^>]*>([^<]+)</span>\s*([^<]*)", details)
                if m:
                    fields[field] = f"{m.group(1).strip()} {m.group(2).strip()}".strip()
            date_m = re.search(r"Posted:\s*([^<]+)", details)
            if date_m:
                fields["Posted"] = date_m.group(1).strip()

        results.append(SearchResult(
            title=title_el.text.strip(),
            link=f"{ABB_BASE_URL}{title_el['href']}",
            format=fields.get("Format", ""),
            bitrate=fields.get("Bitrate", ""),
            file_size=fields.get("File Size", ""),
            posted=fields.get("Posted", ""),
        ))
    return results


def fetch_html(path: str) -> str:
    """GET one page off the site. Raises ABBError on any transport failure."""
    try:
        resp = _get_session().get(f"{ABB_BASE_URL}{path}", timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.ConnectTimeout:
        raise ABBError("AudiobookBay is not responding (connection timed out)")
    except requests.HTTPError as e:
        raise ABBError(f"AudiobookBay returned an error (HTTP {e.response.status_code})")
    except requests.RequestException:
        raise ABBError("AudiobookBay is unreachable (connection failed)")
    return resp.text


def fetch_listing(path: str) -> list[SearchResult]:
    return parse_listing(fetch_html(path))


def search(query: str, max_pages: int = 2) -> list[SearchResult]:
    results: list[SearchResult] = []
    for page in range(1, max_pages + 1):
        if page > 1 or results:
            _delay()

        page_results = fetch_listing(f"/page/{page}/?s={query.lower().replace(' ', '+')}")
        if not page_results:
            break
        results.extend(page_results)

    # Their own search index has been returning nothing for every query since
    # Aug 2026 while the site itself serves fine, so fall back to the local
    # mirror of their listing pages (see abb_index).
    if not results:
        from . import abb_index

        return abb_index.search(query)
    return results


def get_details(result: SearchResult) -> SearchResult:
    session = _get_session()
    _delay()
    resp = session.get(result.link, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    updates: dict = {}

    content = soup.select_one(".postContent")
    if content:
        cover_img = content.find("img")
        if cover_img and cover_img.get("src"):
            src = cover_img["src"]
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = ABB_BASE_URL + src
            updates["cover_url"] = src

        desc_parts: list[str] = []
        for p in content.find_all("p"):
            text = p.get_text(strip=True)
            if not text or text.startswith("Shared by") or p.find("img"):
                continue
            raw = p.get_text("\n", strip=True)
            for line in raw.split("\n"):
                line = line.strip()
                if re.match(
                    r"^(Written|Read|Narrated|Author|Format|Bitrate|Duration|Unabridged|Abridged)\b",
                    line,
                    re.IGNORECASE,
                ):
                    key, _, val = line.partition(":")
                    key = key.strip().lower().replace(" ", "_")
                    val = val.strip()
                    if key in ("read", "read_by", "narrated", "narrated_by"):
                        key = "narrator"
                    if key in ("written", "written_by"):
                        key = "author"
                    if val:
                        updates[key] = val
                    elif key in ("unabridged", "abridged"):
                        updates["abridged"] = key == "abridged"
                elif len(line) > 20:
                    desc_parts.append(line)
        if desc_parts:
            updates["description"] = " ".join(desc_parts)

    post_info = soup.select_one(".postInfo")
    if post_info:
        cat_el = post_info.select_one("a[rel='category tag']")
        if cat_el:
            updates["category"] = cat_el.text.strip()
        lang_el = post_info.select_one("[itemprop='inLanguage']")
        if lang_el:
            updates["language"] = lang_el.text.strip()

    hash_cell = soup.find("td", string=re.compile(r"Info Hash", re.IGNORECASE))
    if hash_cell:
        info_hash = hash_cell.find_next_sibling("td").text.strip()
        tracker_cells = soup.find_all("td", string=re.compile(r"udp://|http://", re.IGNORECASE))
        trackers = [td.text.strip() for td in tracker_cells] or DEFAULT_TRACKERS
        tracker_params = "&".join(f"tr={quote(t)}" for t in trackers)
        updates["magnet"] = f"magnet:?xt=urn:btih:{info_hash}&{tracker_params}"

    valid_fields = {f.name for f in SearchResult.__dataclass_fields__.values()}
    filtered = {k: v for k, v in updates.items() if k in valid_fields}
    return replace(result, **filtered)
