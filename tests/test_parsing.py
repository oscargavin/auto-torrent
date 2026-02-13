import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

from bs4 import BeautifulSoup

from auto_torrent.config import ABB_BASE_URL, DEFAULT_TRACKERS
from auto_torrent.types import SearchResult

FIXTURES = Path(__file__).parent / "fixtures"


def _parse_search_html(html: str) -> list[SearchResult]:
    """Replicates abb.search() parsing logic against raw HTML."""
    soup = BeautifulSoup(html, "html.parser")
    results: list[SearchResult] = []

    for post in soup.select(".post"):
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
            raw = str(details_p)
            for field, attr in [("Format", "format"), ("Bitrate", "bitrate"), ("File Size", "file_size")]:
                m = re.search(rf"{field}:\s*<span[^>]*>([^<]+)</span>\s*([^<]*)", raw)
                if m:
                    val = f"{m.group(1).strip()} {m.group(2).strip()}".strip()
                    if attr == "format":
                        fmt = val
                    elif attr == "bitrate":
                        bitrate = val
                    else:
                        file_size = val
            date_m = re.search(r"Posted:\s*([^<]+)", raw)
            if date_m:
                posted = date_m.group(1).strip()

        results.append(SearchResult(
            title=title, link=link, format=fmt, bitrate=bitrate,
            file_size=file_size, posted=posted,
        ))
    return results


def _parse_detail_html(html: str, base_result: SearchResult) -> SearchResult:
    """Replicates abb.get_details() parsing logic against raw HTML."""
    soup = BeautifulSoup(html, "html.parser")
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
                    line, re.IGNORECASE,
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
    return replace(base_result, **filtered)


class TestSearchParsing:
    def test_parses_all_posts(self):
        html = (FIXTURES / "abb_search.html").read_text()
        results = _parse_search_html(html)
        assert len(results) == 3

    def test_first_result_title(self):
        html = (FIXTURES / "abb_search.html").read_text()
        results = _parse_search_html(html)
        assert results[0].title == "The Wise Man's Fear \u2013 Patrick Rothfuss"

    def test_first_result_link(self):
        html = (FIXTURES / "abb_search.html").read_text()
        results = _parse_search_html(html)
        assert results[0].link == f"{ABB_BASE_URL}/audio-books/the-wise-mans-fear-patrick-rothfuss/"

    def test_metadata_extraction(self):
        html = (FIXTURES / "abb_search.html").read_text()
        results = _parse_search_html(html)
        r = results[0]
        assert "MP3" in r.format
        assert "64" in r.bitrate
        assert "1.2 GB" in r.file_size
        assert "2024-01-15" in r.posted

    def test_second_result_format(self):
        html = (FIXTURES / "abb_search.html").read_text()
        results = _parse_search_html(html)
        assert "M4B" in results[1].format

    def test_no_metadata_still_parses(self):
        html = (FIXTURES / "abb_search.html").read_text()
        results = _parse_search_html(html)
        r = results[2]
        assert r.title == "The Doors of Stone \u2013 Patrick Rothfuss (Fan Reading)"
        assert r.format == ""
        assert r.bitrate == ""


class TestDetailParsing:
    def test_narrator_extracted(self):
        html = (FIXTURES / "abb_detail.html").read_text()
        base = SearchResult(title="Test", link="http://example.com")
        result = _parse_detail_html(html, base)
        assert result.narrator == "Rupert Degas"

    def test_author_extracted(self):
        html = (FIXTURES / "abb_detail.html").read_text()
        base = SearchResult(title="Test", link="http://example.com")
        result = _parse_detail_html(html, base)
        assert result.author == "Patrick Rothfuss"

    def test_magnet_link_built(self):
        html = (FIXTURES / "abb_detail.html").read_text()
        base = SearchResult(title="Test", link="http://example.com")
        result = _parse_detail_html(html, base)
        assert result.magnet.startswith("magnet:?xt=urn:btih:ABCDEF1234567890")

    def test_category_extracted(self):
        html = (FIXTURES / "abb_detail.html").read_text()
        base = SearchResult(title="Test", link="http://example.com")
        result = _parse_detail_html(html, base)
        assert result.category == "Fantasy"

    def test_language_extracted(self):
        html = (FIXTURES / "abb_detail.html").read_text()
        base = SearchResult(title="Test", link="http://example.com")
        result = _parse_detail_html(html, base)
        assert result.language == "English"

    def test_description_extracted(self):
        html = (FIXTURES / "abb_detail.html").read_text()
        base = SearchResult(title="Test", link="http://example.com")
        result = _parse_detail_html(html, base)
        assert "Kvothe" in result.description
        assert "Shared by" not in result.description

    def test_abridged_status(self):
        html = (FIXTURES / "abb_detail.html").read_text()
        base = SearchResult(title="Test", link="http://example.com")
        result = _parse_detail_html(html, base)
        assert result.abridged is False
