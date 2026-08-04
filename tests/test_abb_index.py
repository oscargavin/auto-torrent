import tempfile
from pathlib import Path

import pytest

from auto_torrent import abb_index
from auto_torrent.types import SearchResult


@pytest.fixture
def conn():
    db = Path(tempfile.mkdtemp()) / "index.sqlite3"
    c = abb_index.connect(db)
    yield c
    c.close()


def result(title, link, **kw):
    return SearchResult(title=title, link=f"https://audiobookbay.lu{link}", **kw)


class TestUpsert:
    def test_counts_only_new_links(self, conn):
        first = [result("Frederica - Georgette Heyer", "/abss/frederica/")]
        assert abb_index.upsert(conn, first) == 1
        assert abb_index.upsert(conn, first) == 0
        assert abb_index.count(conn) == 1

    def test_keeps_listing_metadata(self, conn):
        abb_index.upsert(conn, [
            result("Wool - Hugh Howey", "/abss/wool/", format="M4B", file_size="512 MB", posted="3 Aug 2026"),
        ])
        hit = abb_index.search("wool howey", conn=conn)[0]
        assert (hit.format, hit.file_size, hit.posted) == ("M4B", "512 MB", "3 Aug 2026")


class TestSearch:
    def test_matches_words_in_any_order(self, conn):
        abb_index.upsert(conn, [result("Frederica - Georgette Heyer", "/abss/frederica/")])
        for query in ("frederica heyer", "heyer frederica", "georgette frederica"):
            assert len(abb_index.search(query, conn=conn)) == 1, query

    def test_requires_every_word(self, conn):
        abb_index.upsert(conn, [
            result("Frederica - Georgette Heyer", "/abss/frederica/"),
            result("Arabella - Georgette Heyer", "/abss/arabella/"),
        ])
        assert len(abb_index.search("heyer", conn=conn)) == 2
        assert len(abb_index.search("frederica heyer", conn=conn)) == 1

    def test_matches_word_prefixes(self, conn):
        abb_index.upsert(conn, [result("The Hobbit - J.R.R. Tolkien", "/abss/hobbit/")])
        assert len(abb_index.search("hobb tolk", conn=conn)) == 1

    def test_empty_query_finds_nothing(self, conn):
        abb_index.upsert(conn, [result("Wool - Hugh Howey", "/abss/wool/")])
        assert abb_index.search("", conn=conn) == []
        assert abb_index.search("!!", conn=conn) == []


class TestLastPage:
    def test_reads_the_end_from_the_pagination_links(self):
        html = '<a href="/page/2/">2</a><a href="/page/3/">3</a><a href="/page/411/">Last</a>'
        assert abb_index.last_page(html) == 411

    def test_never_exceeds_the_site_wide_cap(self):
        assert abb_index.last_page('<a href="/page/99999/">x</a>') == abb_index.MAX_PAGES

    def test_falls_back_to_a_single_page(self):
        assert abb_index.last_page("<p>no pagination here</p>") == 1


class TestCrawlArchive:
    """An archive's own pagination is the only trustworthy end marker: past the
    end the site re-serves one fixed page forever rather than 404ing, and it
    repeats the odd page mid-archive too."""

    def _fake_site(self, monkeypatch, pages, end=None):
        end = end or max(pages)

        def fake_html(path):
            return f'<a href="/page/{end}/">Last</a>'

        def fake_listing(path):
            return pages.get(_page_number(path), [])

        def fake_parse(html):
            return pages[2]

        monkeypatch.setattr(abb_index, "fetch_html", fake_html)
        monkeypatch.setattr(abb_index, "parse_listing", fake_parse)
        monkeypatch.setattr(abb_index, "fetch_listing", fake_listing)

    def test_crawls_to_the_advertised_last_page(self, conn, monkeypatch):
        pages = {p: [result(f"Book {p}", f"/abss/book-{p}/")] for p in range(2, 8)}
        self._fake_site(monkeypatch, pages, end=7)
        assert abb_index.crawl_archive(conn, "/audio-books/type/classic", workers=2) == (6, 6)

    def test_keeps_going_past_a_repeated_page(self, conn, monkeypatch):
        """The site duplicates the odd page mid-archive — that is not the end."""
        pages = {
            2: [result("A", "/abss/a/")],
            3: [result("A", "/abss/a/")],  # identical to page 2
            4: [result("B", "/abss/b/")],
        }
        self._fake_site(monkeypatch, pages, end=4)
        crawled, new = abb_index.crawl_archive(conn, "", workers=1)
        assert (crawled, new) == (3, 2)

    def test_rides_out_a_transient_empty_page(self, conn, monkeypatch):
        pages = {2: [result("Book 2", "/abss/book-2/")],
                 3: [],  # a failed fetch, not the end of the archive
                 4: [result("Book 4", "/abss/book-4/")]}
        self._fake_site(monkeypatch, pages, end=9)
        crawled, new = abb_index.crawl_archive(conn, "", workers=1)
        assert (crawled, new) == (2, 2)

    def test_gives_up_after_a_run_of_empty_pages(self, conn, monkeypatch):
        pages = {2: [result("Book 2", "/abss/book-2/")]}
        self._fake_site(monkeypatch, pages, end=99)
        crawled, _ = abb_index.crawl_archive(conn, "", workers=1)
        assert crawled == 1

    def test_refresh_stops_once_everything_is_known(self, conn, monkeypatch):
        pages = {p: [result(f"Book {p}", f"/abss/book-{p}/")] for p in range(2, 9)}
        self._fake_site(monkeypatch, pages, end=8)
        abb_index.crawl_archive(conn, "", workers=1)

        crawled, new = abb_index.crawl_archive(conn, "", stop_when_known=True, workers=1)
        assert (crawled, new) == (1, 0)  # first page held nothing new: stop


def _page_number(path: str) -> int:
    return int(path.rstrip("/").split("/")[-1])
