import re
from dataclasses import replace
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from .config import ABB_BASE_URL, DEFAULT_TRACKERS, HEADERS
from .types import SearchResult


class ABBError(Exception):
    pass


def search(query: str, max_pages: int = 2) -> list[SearchResult]:
    results: list[SearchResult] = []
    for page in range(1, max_pages + 1):
        url = f"{ABB_BASE_URL}/page/{page}/?s={query.lower().replace(' ', '+')}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
        except requests.ConnectTimeout:
            raise ABBError("AudiobookBay is not responding (connection timed out)")
        except requests.ConnectionError:
            raise ABBError("AudiobookBay is unreachable (connection failed)")
        except requests.HTTPError as e:
            raise ABBError(f"AudiobookBay returned an error (HTTP {e.response.status_code})")
        except requests.RequestException:
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        posts = soup.select(".post")
        if not posts:
            break

        for post in posts:
            title_el = post.select_one(".postTitle > h2 > a")
            if not title_el:
                continue

            title = title_el.text.strip()
            link = f"{ABB_BASE_URL}{title_el['href']}"

            fmt = ""
            bitrate = ""
            file_size = ""
            posted = ""

            details_p = post.select_one(".postContent p[style*='text-align:center']")
            if details_p:
                html = str(details_p)
                for field, attr in [("Format", "format"), ("Bitrate", "bitrate"), ("File Size", "file_size")]:
                    m = re.search(rf"{field}:\s*<span[^>]*>([^<]+)</span>\s*([^<]*)", html)
                    if m:
                        val = f"{m.group(1).strip()} {m.group(2).strip()}".strip()
                        if attr == "format":
                            fmt = val
                        elif attr == "bitrate":
                            bitrate = val
                        else:
                            file_size = val
                date_m = re.search(r"Posted:\s*([^<]+)", html)
                if date_m:
                    posted = date_m.group(1).strip()

            results.append(SearchResult(
                title=title,
                link=link,
                format=fmt,
                bitrate=bitrate,
                file_size=file_size,
                posted=posted,
            ))
    return results


def get_details(result: SearchResult) -> SearchResult:
    resp = requests.get(result.link, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    updates: dict = {}

    content = soup.select_one(".postContent")
    if content:
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

    # Only pass fields that exist on SearchResult
    valid_fields = {f.name for f in SearchResult.__dataclass_fields__.values()}
    filtered = {k: v for k, v in updates.items() if k in valid_fields}
    return replace(result, **filtered)
