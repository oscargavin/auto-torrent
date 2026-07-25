"""Metadata resolution.

The bug these exist for: asked for "The Poppy War by R.F. Kuang", the agent
announced it was looking for *The Dragon Republic* — the sequel. OpenLibrary
ranked it first (same author, same series, same subjects) and lookup_book took
docs[0] on trust. It resolved correctly on a retry, so it presented as
intermittent rather than as a missing check.
"""
from unittest.mock import patch

from auto_torrent import openlibrary as ol


def _doc(title, authors, **extra):
    return {"title": title, "author_name": authors, **extra}


# The observed response, in the observed order.
POPPY_WAR_DOCS = [
    _doc("The Dragon Republic", ["R. F. Kuang"], first_publish_year=2019),
    _doc("The Poppy War", ["R. F. Kuang"], first_publish_year=2018, cover_i=9),
    _doc("The Burning God", ["R. F. Kuang"], first_publish_year=2020),
]


class TestSplitTitleAuthor:
    def test_splits_on_by(self):
        assert ol._split_title_author("The Poppy War by R.F. Kuang") == (
            "The Poppy War",
            "R.F. Kuang",
        )

    def test_splits_on_the_last_by_so_titles_may_contain_it(self):
        assert ol._split_title_author("Gone by Midnight by Jane Harper") == (
            "Gone by Midnight",
            "Jane Harper",
        )

    def test_no_author_when_the_query_is_a_bare_title(self):
        assert ol._split_title_author("Dune") == ("Dune", "")

    def test_a_dangling_by_is_not_an_author(self):
        assert ol._split_title_author("Dune by ") == ("Dune by", "")


class TestPickBest:
    def test_prefers_the_requested_book_over_a_sequel_by_the_same_author(self):
        best = ol._pick_best(POPPY_WAR_DOCS, "The Poppy War", "R.F. Kuang")
        assert best["title"] == "The Poppy War"

    def test_matches_an_author_whose_initials_are_punctuated_differently(self):
        # "R.F. Kuang" vs "R. F. Kuang" — never equal, so the surname carries it.
        assert ol._author_score(["R. F. Kuang"], "R.F. Kuang") > 0

    def test_ignores_articles(self):
        best = ol._pick_best(
            [_doc("Hobbit, The", ["Tolkien"]), _doc("The Silmarillion", ["Tolkien"])],
            "The Hobbit",
            "Tolkien",
        )
        assert best["title"] == "Hobbit, The"

    def test_an_exact_title_beats_a_longer_one_that_starts_with_it(self):
        best = ol._pick_best(
            [_doc("Dune Messiah", ["Frank Herbert"]), _doc("Dune", ["Frank Herbert"])],
            "Dune",
            "Frank Herbert",
        )
        assert best["title"] == "Dune"

    def test_keeps_relevance_order_when_nothing_distinguishes_them(self):
        docs = [_doc("First", ["A"]), _doc("Second", ["A"])]
        assert ol._pick_best(docs, "unrelated", "")["title"] == "First"

    def test_the_right_author_beats_the_right_title_by_someone_else(self):
        # A different author's book of the same name is the wrong book.
        best = ol._pick_best(
            [_doc("The Poppy War", ["Someone Else"]), _doc("The Poppy War", ["R. F. Kuang"])],
            "The Poppy War",
            "R.F. Kuang",
        )
        assert best["author_name"] == ["R. F. Kuang"]


class TestLookupBook:
    def test_uses_a_structured_search_when_an_author_is_given(self):
        seen = {}

        def fake(q="", *, title="", author=""):
            seen.update({"q": q, "title": title, "author": author})
            return POPPY_WAR_DOCS

        with patch.object(ol, "_try_query", fake):
            book = ol.lookup_book("The Poppy War by R.F. Kuang")

        # Structured, not the whole sentence thrown at `q`.
        assert seen == {"q": "", "title": "The Poppy War", "author": "R.F. Kuang"}
        assert book.title == "The Poppy War"
        assert book.author == "R. F. Kuang"

    def test_falls_back_to_a_plain_query_when_structured_finds_nothing(self):
        calls = []

        def fake(q="", *, title="", author=""):
            calls.append((q, title, author))
            return [] if title else POPPY_WAR_DOCS

        with patch.object(ol, "_try_query", fake):
            book = ol.lookup_book("The Poppy War by R.F. Kuang")

        assert len(calls) > 1
        # Still scored, not just docs[0] — the fallback path was the buggy one.
        assert book.title == "The Poppy War"

    def test_a_structured_search_failing_does_not_fail_the_lookup(self):
        import requests

        def fake(q="", *, title="", author=""):
            if title:
                raise requests.RequestException("boom")
            return POPPY_WAR_DOCS

        with patch.object(ol, "_try_query", fake):
            assert ol.lookup_book("The Poppy War by R.F. Kuang").title == "The Poppy War"

    def test_returns_none_when_nothing_matches(self):
        with patch.object(ol, "_try_query", lambda *a, **k: []):
            assert ol.lookup_book("asdkjhaskdjh") is None

    def test_bare_title_query_still_works(self):
        with patch.object(ol, "_try_query", lambda *a, **k: [_doc("Dune", ["Frank Herbert"])]):
            book = ol.lookup_book("Dune")
        assert book.title == "Dune"
        assert book.author == "Frank Herbert"
