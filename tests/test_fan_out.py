"""The fan-out fetches its listings at once, but the merged list must not
depend on which listing came back first."""
import threading
import time
from unittest.mock import patch

import pytest

from auto_torrent import cli
from auto_torrent.abb import ABBError
from auto_torrent.types import BookMetadata, SearchResult


def _r(slug: str) -> SearchResult:
    return SearchResult(title=slug, link=f"https://abb/{slug}")


# The first query is the slowest, so a merge in completion order would differ.
LISTINGS = {
    "Dune": (0.15, [_r("dune"), _r("dune-messiah")]),
    "Dune Frank Herbert": (0.05, [_r("dune-messiah"), _r("dune-ga")]),
    "Frank Herbert": (0.0, [_r("whipping-star"), _r("dune")]),
}


def _fake_search(calls: list[str]):
    lock = threading.Lock()

    def search(query: str, max_pages: int = 2) -> list[SearchResult]:
        with lock:
            calls.append(query)
        delay, results = LISTINGS[query]
        time.sleep(delay)
        return results

    return search


BOOK = BookMetadata(title="Dune", author="Frank Herbert", series="Dune")


def test_keeps_the_sequential_order_and_drops_repeats():
    calls: list[str] = []
    with patch.object(cli.abb, "search", _fake_search(calls)):
        results = cli._fan_out_search(BOOK, raw_query="Dune Frank Herbert")
    assert [r.title for r in results] == ["dune", "dune-messiah", "dune-ga", "whipping-star"]


def test_a_series_equal_to_the_title_is_searched_once():
    calls: list[str] = []
    with patch.object(cli.abb, "search", _fake_search(calls)):
        cli._fan_out_search(BOOK, raw_query="Dune Frank Herbert")
    assert sorted(calls) == ["Dune", "Dune Frank Herbert", "Frank Herbert"]


def test_the_listings_are_fetched_at_once():
    start = time.perf_counter()
    with patch.object(cli.abb, "search", _fake_search([])):
        cli._fan_out_search(BOOK, raw_query="Dune Frank Herbert")
    # Sequential would be 0.20 s; concurrent is the slowest one, 0.15 s.
    assert time.perf_counter() - start < 0.19


def test_a_failing_listing_still_raises():
    def search(query: str, max_pages: int = 2) -> list[SearchResult]:
        if query == "Frank Herbert":
            raise ABBError("AudiobookBay is unreachable (connection failed)")
        return LISTINGS[query][1]

    with patch.object(cli.abb, "search", search), pytest.raises(ABBError):
        cli._fan_out_search(BOOK, raw_query="Dune Frank Herbert")


def test_concurrent_index_fallbacks_do_not_mix_rows(tmp_path):
    # AudiobookBay's own search returns nothing, so every fan-out query falls
    # back to the shared sqlite index at the same time. Without a lock the
    # threads read rows from each other's cursors.
    from auto_torrent import abb, abb_index

    conn = abb_index.connect(tmp_path / "index.sqlite")
    words = ["dune", "herbert", "messiah", "frank"]
    abb_index.upsert(
        conn,
        [SearchResult(title=f"{w} book {i}", link=f"https://abb/{w}-{i}") for w in words for i in range(200)],
    )
    book = BookMetadata(title="dune", author="frank", series="messiah")
    with (
        patch.object(abb_index, "_conn", conn),
        patch.object(abb, "fetch_listing", lambda path: []),
    ):
        for _ in range(30):
            results = cli._fan_out_search(book, raw_query="herbert")
            assert all(r.title for r in results)
            assert len(results) == 80
