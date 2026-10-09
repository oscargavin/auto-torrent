"""Metadata resolution.

The bug these exist for: asked for "The Poppy War by R.F. Kuang", the agent
announced it was looking for *The Dragon Republic* — the sequel. OpenLibrary
ranked it first (same author, same series, same subjects) and lookup_book took
docs[0] on trust. It resolved correctly on a retry, so it presented as
intermittent rather than as a missing check.
"""
from unittest.mock import patch

import pytest

from auto_torrent import openlibrary as ol


@pytest.fixture(autouse=True)
def _empty_lookup_cache():
    ol._cache.clear()
    yield
    ol._cache.clear()


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


class TestCollections:
    """A box set legitimately matches the title of every book inside it, and
    downloading one costs gigabytes and yields an item that isn't the book
    that was asked for. Observed live for both of these queries."""

    def test_prefers_the_single_book_over_a_box_set(self):
        best = ol._pick_best(
            [
                _doc(
                    "Poppy War Trilogy 3-Book Set by R. F. Kuang "
                    "(the Poppy War, the Dragon Republic, the Burning God)",
                    ["R. F. Kuang"],
                ),
                _doc("The Poppy War", ["R. F. Kuang"]),
            ],
            "The Poppy War",
            "R.F. Kuang",
        )
        assert best["title"] == "The Poppy War"

    def test_prefers_the_single_book_over_an_omnibus(self):
        best = ol._pick_best(
            [
                _doc("Dune, Dune Messiah, Children of Dune", ["Frank Herbert"]),
                _doc("Dune", ["Frank Herbert"]),
            ],
            "Dune",
            "Frank Herbert",
        )
        assert best["title"] == "Dune"

    def test_an_omnibus_still_wins_when_the_user_asked_for_one(self):
        best = ol._pick_best(
            [
                _doc("Dune", ["Frank Herbert"]),
                _doc("The Dune Trilogy", ["Frank Herbert"]),
            ],
            "The Dune Trilogy",
            "Frank Herbert",
        )
        assert best["title"] == "The Dune Trilogy"

    def test_a_sequel_scores_below_the_book_but_above_a_collection(self):
        exact = ol._title_score("Dune", "Dune")
        sequel = ol._title_score("Dune Messiah", "Dune")
        omnibus = ol._title_score("Dune, Dune Messiah, Children of Dune", "Dune")
        assert exact > sequel > omnibus


# The observed plain-query response for "Dune Frank Herbert", in the observed
# order. lookup_book scored the whole sentence as the title, so "Dune Messiah"
# won on shared words, and its subject "American Science fiction" became the
# series that the fan-out then searched.
DUNE_DOCS = [
    _doc(
        "Dune",
        ["Frank Herbert"],
        series_name=["Dune"],
        subject=["Dune (Imaginary place)", "Fiction"],
    ),
    _doc(
        "Dune Messiah",
        ["Frank Herbert"],
        series_name=["Dune"],
        subject=["American Science fiction", "Dune (Imaginary place)"],
    ),
    _doc("Children of Dune", ["Frank Herbert"], series_name=["Dune"]),
    _doc("Dune, Dune Messiah, Children of Dune", ["Frank Herbert"]),
]


def _plain_only(docs):
    """A fake _try_query that answers the plain query and nothing else."""

    def fake(q="", *, title="", author=""):
        return docs if q else []

    return fake


class TestAuthorInTheQuery:
    """Agents send "Title Author" with no " by " between them."""

    def test_the_author_words_are_not_scored_as_title(self):
        with patch.object(ol, "_try_query", _plain_only(DUNE_DOCS)):
            book = ol.lookup_book("Dune Frank Herbert")
        assert book.title == "Dune"
        assert book.author == "Frank Herbert"

    def test_the_author_may_come_first(self):
        with patch.object(ol, "_try_query", _plain_only(DUNE_DOCS)):
            assert ol.lookup_book("Frank Herbert Dune").title == "Dune"

    def test_a_summary_does_not_beat_the_book(self):
        docs = [
            _doc("Summary of Project Hail Mary by Andy Weir", ["Antoine McCallan"]),
            _doc("The Martian / Artemis / Project Hail Mary", ["Andy Weir"]),
            _doc("Project Hail Mary", ["Andy Weir"]),
        ]
        with patch.object(ol, "_try_query", _plain_only(docs)):
            book = ol.lookup_book("Project Hail Mary Andy Weir")
        assert (book.title, book.author) == ("Project Hail Mary", "Andy Weir")

    def test_apostrophes_and_initials_still_match(self):
        docs = [_doc("Assassin’s Apprentice", ["Robin Hobb"])]
        with patch.object(ol, "_try_query", _plain_only(docs)):
            assert ol.lookup_book("Assassin's Apprentice Robin Hobb").author == "Robin Hobb"
        assert ol._strip_author("The Poppy War R.F. Kuang", "R. F. Kuang") == "The Poppy War"

    def test_asks_again_when_the_plain_query_misses_the_single_book(self):
        calls = []

        def fake(q="", *, title="", author=""):
            calls.append((q, title, author))
            if q:
                return [_doc("The Poppy War / The Dragon Republic / The Burning God", ["R. F. Kuang"])]
            return POPPY_WAR_DOCS

        with patch.object(ol, "_try_query", fake):
            book = ol.lookup_book("The Poppy War R.F. Kuang")
        assert book.title == "The Poppy War"
        assert ("", "The Poppy War", "R. F. Kuang") in calls

    def test_a_whole_title_is_not_cut_by_a_stray_author(self):
        # Observed: a doc credited to "Book Of Thrones" made "of thrones" look
        # like an author at the end of the query.
        docs = [
            _doc("A Game of Thrones", ["George R. R. Martin"]),
            _doc("Game of Thrones", ["Book Of Thrones"]),
        ]
        with patch.object(ol, "_try_query", _plain_only(docs)):
            book = ol.lookup_book("A Game of Thrones")
        assert (book.title, book.author) == ("A Game of Thrones", "George R. R. Martin")

    def test_an_author_alone_is_not_a_title(self):
        docs = [_doc("The Worlds of Frank Herbert", ["Frank Herbert"]), *DUNE_DOCS]
        with patch.object(ol, "_try_query", _plain_only(docs)):
            assert ol.lookup_book("Frank Herbert") is None


class TestSeries:
    def test_uses_series_name_never_a_subject(self):
        with patch.object(ol, "_try_query", _plain_only(DUNE_DOCS)):
            assert ol.lookup_book("Dune Frank Herbert").series == "Dune"

    def test_no_series_when_openlibrary_has_none(self):
        docs = [_doc("Project Hail Mary", ["Andy Weir"], subject=["hard science-fiction", "Fiction"])]
        with patch.object(ol, "_try_query", _plain_only(docs)):
            assert ol.lookup_book("Project Hail Mary").series is None

    def test_a_franchise_subject_still_counts(self):
        docs = [_doc("Star Wars: Heir to the Empire", ["Timothy Zahn"], subject=["Fiction", "franchise:Star Wars"])]
        with patch.object(ol, "_try_query", _plain_only(docs)):
            assert ol.lookup_book("Star Wars: Heir to the Empire").series == "Star Wars"


class TestLookupCache:
    def test_a_repeat_lookup_does_not_ask_again(self):
        calls = []

        def fake(q="", *, title="", author=""):
            calls.append(q)
            return DUNE_DOCS if q else []

        with patch.object(ol, "_try_query", fake):
            first = ol.lookup_book("Dune Frank Herbert")
            n = len(calls)
            assert ol.lookup_book("  dune frank HERBERT ") == first
        assert len(calls) == n

    def test_entries_expire(self):
        calls = []

        def fake(q="", *, title="", author=""):
            calls.append(q)
            return DUNE_DOCS if q else []

        with patch.object(ol, "_try_query", fake):
            ol.lookup_book("Dune Frank Herbert")
            n = len(calls)
            with patch.object(ol, "_CACHE_TTL", -1):
                ol.lookup_book("Dune Frank Herbert")
        assert len(calls) > n

    def test_a_failure_is_not_cached(self):
        import requests

        def boom(*a, **k):
            raise requests.RequestException("down")

        with patch.object(ol, "_try_query", boom), pytest.raises(requests.RequestException):
            ol.lookup_book("Dune")
        with patch.object(ol, "_try_query", _plain_only(DUNE_DOCS)):
            assert ol.lookup_book("Dune").title == "Dune"


class TestSubtitledQueries:
    def test_the_book_after_the_colon_beats_an_omnibus_listing_every_word(self):
        docs = [
            _doc("The Final Empire", ["Brandon Sanderson"]),
            _doc("Mistborn", ["Brandon Sanderson"]),
            _doc(
                "Brandon Sanderson's Fantasy Firsts : (the Way of Kings, "
                "Mistborn: the Final Empire, Rithmatist)",
                ["Brandon Sanderson"],
            ),
        ]
        with patch.object(ol, "_try_query", _plain_only(docs)):
            assert ol.lookup_book("Mistborn: The Final Empire").title == "The Final Empire"

    def test_a_whole_title_with_a_colon_still_wins(self):
        best = ol._pick_best(
            [_doc("Dune", ["Frank Herbert"]), _doc("Dune: Part One", ["Frank Herbert"])],
            "Dune: Part One",
            "",
        )
        assert best["title"] == "Dune: Part One"
