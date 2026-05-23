"""TDD for the recommendation hydration core (auto_torrent/audnex.py).

Pipeline under test: an LLM hands us a (possibly imperfect) `title + author`;
we turn it into a rich `BookCard` with a SQUARE Audible cover via Audible
catalog search → best-match → Audnexus enrich, falling back to the Audible
search record itself, then Open Library, then None (= hallucination filter).

HTTP is mocked everywhere — these are deterministic unit tests.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from auto_torrent.audnex import (
    best_match,
    fetch_audnex,
    hydrate,
    match_key,
    parse_audible_product,
    parse_book,
    search_audible,
    sized_cover,
)
from auto_torrent.types import BookCard

# --- realistic fixtures (trimmed from live API responses) ----------------

AUDNEX_PHM = {
    "asin": "B08GB2RLKM",
    "title": "Project Hail Mary",
    "subtitle": None,
    "description": "When the Sun is threatened by an alien microbe…",
    "image": "https://m.media-amazon.com/images/I/81Nzlrfud+L.jpg",
    "publisherName": "Audible Studios",
    "releaseDate": "2021-05-04T00:00:00.000Z",
    "runtimeLengthMin": 970,
    "formatType": "unabridged",
    "language": "english",
    "isbn": "9781603935470",
    "authors": [{"asin": "B00G0WYW92", "name": "Andy Weir"}],
    "narrators": [{"name": "Ray Porter"}],
    "seriesPrimary": None,
    "genres": [
        {"asin": "1", "name": "Science Fiction & Fantasy", "type": "genre"},
        {"asin": "2", "name": "Hard Science Fiction", "type": "tag"},
    ],
}

AUDNEX_SERIES = {
    "asin": "B017V4IM1G",
    "title": "The Wise Man's Fear",
    "description": "Day Two of the Kingkiller Chronicle…",
    "image": "https://m.media-amazon.com/images/I/91abcDEf.jpg",
    "releaseDate": "2011-03-01T00:00:00.000Z",
    "runtimeLengthMin": 2490,
    "authors": [{"name": "Patrick Rothfuss"}],
    "narrators": [{"name": "Rupert Degas"}],
    "seriesPrimary": {"asin": "S1", "name": "The Kingkiller Chronicle", "position": "2"},
    "genres": [{"asin": "1", "name": "Fantasy", "type": "genre"}],
}

AUDIBLE_PHM = {
    "asin": "B08GB2RLKM",
    "title": "Project Hail Mary",
    "authors": [{"asin": "B00G0WYW92", "name": "Andy Weir"}],
    "narrators": [{"name": "Ray Porter"}],
    "merchandising_summary": "<p>When the Sun is threatened…</p>",
    "product_images": {
        "500": "https://m.media-amazon.com/images/I/51POf8gOyLL._SL500_.jpg",
        "1024": "https://m.media-amazon.com/images/I/51POf8gOyLL._SL1024_.jpg",
    },
    "release_date": "2021-05-04",
    "runtime_length_min": 970,
    "series": None,
}

AUDIBLE_DISTRACTOR = {
    "asin": "BXXX",
    "title": "The Silent Patient",
    "authors": [{"name": "Alex Michaelides"}],
    "narrators": [{"name": "Jack Hawkins"}],
    "product_images": {"500": "https://m.media-amazon.com/images/I/zzz._SL500_.jpg"},
    "release_date": "2019-02-05",
}


def _resp(json_data, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data
    r.raise_for_status.return_value = None
    return r


# --- match_key -----------------------------------------------------------

class TestMatchKey:
    def test_normalises_punctuation_and_case(self):
        assert match_key("The Hobbit", "J.R.R. Tolkien") == match_key(
            "the  hobbit!", "jrr  tolkien"
        )

    def test_strips_leading_article(self):
        assert match_key("The Hobbit", "Tolkien") == match_key("Hobbit", "Tolkien")

    def test_different_books_differ(self):
        assert match_key("Project Hail Mary", "Andy Weir") != match_key(
            "The Martian", "Andy Weir"
        )


# --- sized_cover ---------------------------------------------------------

class TestSizedCover:
    def test_inserts_token_before_extension(self):
        assert (
            sized_cover("https://m.media-amazon.com/images/I/81Nzlrfud+L.jpg", 500)
            == "https://m.media-amazon.com/images/I/81Nzlrfud+L._SL500_.jpg"
        )

    def test_replaces_existing_token(self):
        assert (
            sized_cover("https://m.media-amazon.com/images/I/51POf8gOyLL._SL500_.jpg", 300)
            == "https://m.media-amazon.com/images/I/51POf8gOyLL._SL300_.jpg"
        )

    def test_leaves_non_amazon_url_untouched(self):
        url = "https://covers.openlibrary.org/b/id/123-L.jpg"
        assert sized_cover(url, 500) == url

    def test_none_passthrough(self):
        assert sized_cover(None, 500) is None


# --- best_match (rapidfuzz over Audible search results) ------------------

class TestBestMatch:
    def test_exact_match_wins(self):
        m = best_match([AUDIBLE_PHM], "Project Hail Mary", "Andy Weir")
        assert m is AUDIBLE_PHM

    def test_picks_correct_among_distractors(self):
        m = best_match(
            [AUDIBLE_DISTRACTOR, AUDIBLE_PHM], "Project Hail Mary", "Andy Weir"
        )
        assert m is AUDIBLE_PHM

    def test_author_disambiguates_same_title(self):
        becoming_a = {"title": "Becoming", "authors": [{"name": "Michelle Obama"}]}
        becoming_b = {"title": "Becoming", "authors": [{"name": "David Kessler"}]}
        m = best_match([becoming_b, becoming_a], "Becoming", "Michelle Obama")
        assert m is becoming_a

    def test_typo_in_title_still_matches(self):
        m = best_match([AUDIBLE_PHM], "Projict Hail Mary", "Andy Weir")
        assert m is AUDIBLE_PHM

    def test_all_irrelevant_returns_none(self):
        m = best_match([AUDIBLE_DISTRACTOR], "Project Hail Mary", "Andy Weir")
        assert m is None

    def test_empty_results_returns_none(self):
        assert best_match([], "Anything", "Anyone") is None


# --- parse_book (Audnexus → BookCard) -----------------------------------

class TestParseBook:
    def test_extracts_core_fields(self):
        card = parse_book(AUDNEX_PHM)
        assert isinstance(card, BookCard)
        assert card.title == "Project Hail Mary"
        assert card.author == "Andy Weir"
        assert card.asin == "B08GB2RLKM"
        assert card.narrators == ("Ray Porter",)
        assert card.description.startswith("When the Sun")
        assert card.runtime_min == 970
        assert card.year == 2021
        assert card.source == "audible"

    def test_cover_is_sized_square_amazon_url(self):
        card = parse_book(AUDNEX_PHM, cover_px=500)
        assert card.cover_url == (
            "https://m.media-amazon.com/images/I/81Nzlrfud+L._SL500_.jpg"
        )

    def test_genres_keeps_only_genre_type(self):
        card = parse_book(AUDNEX_PHM)
        assert card.genres == ("Science Fiction & Fantasy",)

    def test_extracts_series_and_position(self):
        card = parse_book(AUDNEX_SERIES)
        assert card.series == "The Kingkiller Chronicle"
        assert card.series_position == "2"

    def test_prefers_full_summary_over_short_description(self):
        data = {
            **AUDNEX_PHM,
            "summary": "<p>The <b>full</b> blurb, much longer than the teaser.</p>",
            "description": "Short teaser…",
        }
        card = parse_book(data)
        assert card.description == "The full blurb, much longer than the teaser."

    def test_decodes_html_entities_in_summary(self):
        card = parse_book({**AUDNEX_PHM, "summary": "<p>She said &quot;hi&quot; &amp; left.</p>"})
        assert card.description == 'She said "hi" & left.'

    def test_br_tags_become_paragraph_breaks(self):
        card = parse_book({**AUDNEX_PHM, "summary": "First para.<br /><br />Second para."})
        assert card.description == "First para.\n\nSecond para."

    def test_trims_other_books_tail(self):
        synopsis = (
            "A genuinely long synopsis that comfortably clears the one hundred and fifty "
            "character guard, padded with sufficient extra words so the trimmer reliably "
            "engages on the promotional block that follows it below."
        )
        summary = f"{synopsis}<br /><br /><b>Other books by Someone</b><br /><i>Book One</i>"
        card = parse_book({**AUDNEX_PHM, "summary": summary})
        assert "Other books" not in card.description
        assert card.description.startswith("A genuinely long synopsis")

    def test_trims_star_review_spam(self):
        synopsis = (
            "Another sufficiently long synopsis sentence written to go well beyond the one "
            "hundred and fifty character guard threshold, ensuring the starred review spam "
            "appended after it gets removed cleanly from the text."
        )
        card = parse_book(
            {**AUDNEX_PHM, "summary": f"{synopsis}<br />⭐ ⭐ ⭐ ⭐ ⭐ Goodreads reviewer"}
        )
        assert "⭐" not in card.description and "Goodreads" not in card.description

    def test_falls_back_to_description_when_no_summary(self):
        card = parse_book(AUDNEX_PHM)  # fixture has description, no summary
        assert card.description.startswith("When the Sun")

    def test_missing_optional_fields_do_not_crash(self):
        card = parse_book({"title": "Bare", "authors": [{"name": "A"}]})
        assert card.title == "Bare"
        assert card.narrators == ()
        assert card.series is None
        assert card.year is None
        assert card.cover_url is None
        assert card.genres == ()


# --- parse_audible_product (fallback when Audnexus 404s) -----------------

class TestParseAudibleProduct:
    def test_builds_card_from_search_record(self):
        card = parse_audible_product(AUDIBLE_PHM)
        assert card.title == "Project Hail Mary"
        assert card.author == "Andy Weir"
        assert card.narrators == ("Ray Porter",)
        assert card.year == 2021
        assert card.runtime_min == 970

    def test_uses_largest_image_and_strips_html(self):
        card = parse_audible_product(AUDIBLE_PHM, cover_px=500)
        assert card.cover_url == (
            "https://m.media-amazon.com/images/I/51POf8gOyLL._SL500_.jpg"
        )
        assert "<p>" not in card.description and card.description.startswith("When the Sun")


# --- search_audible / fetch_audnex (thin HTTP, mocked) ------------------

class TestSearchAudible:
    def test_uses_uk_tld_and_passes_query(self):
        with patch("auto_torrent.audnex.requests.get") as g:
            g.return_value = _resp({"products": [AUDIBLE_PHM]})
            out = search_audible("Project Hail Mary", "Andy Weir", region="uk")
        url = g.call_args.args[0]
        params = g.call_args.kwargs["params"]
        assert "api.audible.co.uk" in url
        assert params["title"] == "Project Hail Mary"
        assert params["author"] == "Andy Weir"
        assert out == [AUDIBLE_PHM]

    def test_us_region_uses_com_tld(self):
        with patch("auto_torrent.audnex.requests.get") as g:
            g.return_value = _resp({"products": []})
            search_audible("X", region="us")
        assert "api.audible.com/" in g.call_args.args[0]


class TestFetchAudnex:
    def test_returns_json_on_200(self):
        with patch("auto_torrent.audnex.requests.get") as g:
            g.return_value = _resp(AUDNEX_PHM)
            assert fetch_audnex("B08GB2RLKM", "uk")["asin"] == "B08GB2RLKM"

    def test_404_returns_none(self):
        with patch("auto_torrent.audnex.requests.get") as g:
            g.return_value = _resp({}, status=404)
            assert fetch_audnex("BAD", "uk") is None

    def test_region_unavailable_error_body_returns_none(self):
        with patch("auto_torrent.audnex.requests.get") as g:
            g.return_value = _resp({"error": {"code": "REGION_UNAVAILABLE"}})
            assert fetch_audnex("B017V4IM1G", "uk") is None


# --- hydrate (orchestration) --------------------------------------------

class TestHydrate:
    def test_happy_path_returns_full_card(self):
        with (
            patch("auto_torrent.audnex.search_audible", return_value=[AUDIBLE_PHM]),
            patch("auto_torrent.audnex.fetch_audnex", return_value=AUDNEX_PHM),
        ):
            card = hydrate("Project Hail Mary", "Andy Weir")
        assert card is not None
        assert card.asin == "B08GB2RLKM"
        assert card.cover_url.startswith("https://m.media-amazon.com")
        assert card.narrators == ("Ray Porter",)

    def test_audnex_miss_falls_back_to_audible_record(self):
        with (
            patch("auto_torrent.audnex.search_audible", return_value=[AUDIBLE_PHM]),
            patch("auto_torrent.audnex.fetch_audnex", return_value=None),
        ):
            card = hydrate("Project Hail Mary", "Andy Weir")
        assert card is not None
        assert card.title == "Project Hail Mary"
        assert card.cover_url is not None  # from product_images

    def test_no_audible_match_uses_openlibrary_fallback(self):
        from auto_torrent.types import BookMetadata

        ol = BookMetadata(title="Obscure Zine", author="Indie Author", cover_id=999)
        with (
            patch("auto_torrent.audnex.search_audible", return_value=[AUDIBLE_DISTRACTOR]),
            patch("auto_torrent.audnex.lookup_book", return_value=ol),
        ):
            card = hydrate("Obscure Zine", "Indie Author")
        assert card is not None
        assert card.source == "openlibrary"
        assert "openlibrary.org" in card.cover_url

    def test_total_miss_returns_none(self):
        with (
            patch("auto_torrent.audnex.search_audible", return_value=[]),
            patch("auto_torrent.audnex.lookup_book", return_value=None),
        ):
            assert hydrate("Definitely Not A Real Book 9xq", "Nobody") is None

    def test_search_network_error_falls_through_to_fallback(self):
        import requests as _rq

        with (
            patch("auto_torrent.audnex.search_audible", side_effect=_rq.RequestException()),
            patch("auto_torrent.audnex.lookup_book", return_value=None),
        ):
            assert hydrate("X", "Y") is None
