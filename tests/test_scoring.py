from auto_torrent.scoring import score_and_sort, score_result
from auto_torrent.types import BookMetadata, SearchResult


def _book(**kw) -> BookMetadata:
    defaults = {"title": "The Wise Man's Fear", "author": "Patrick Rothfuss"}
    return BookMetadata(**(defaults | kw))


def _result(**kw) -> SearchResult:
    defaults = {"title": "The Wise Man's Fear", "link": "http://example.com/1", "magnet": "magnet:?xt=urn:btih:abc"}
    return SearchResult(**(defaults | kw))


class TestScoreResult:
    def test_exact_title_and_author(self):
        score = score_result(
            _result(title="The Wise Man's Fear – Patrick Rothfuss"),
            _book(),
        )
        assert score >= 80

    def test_wrong_title_low_score(self):
        score = score_result(
            _result(title="Harry Potter and the Goblet of Fire"),
            _book(),
        )
        assert score < 50

    def test_narrator_match_boosts(self):
        base = score_result(
            _result(title="The Wise Man's Fear", narrator=""),
            _book(),
        )
        boosted = score_result(
            _result(title="The Wise Man's Fear", narrator="Rupert Degas"),
            _book(),
            prefer_narrator="Rupert Degas",
        )
        assert boosted > base

    def test_narrator_missing_penalizes(self):
        with_narrator = score_result(
            _result(title="The Wise Man's Fear", narrator="Someone Else"),
            _book(),
            prefer_narrator="Rupert Degas",
        )
        without_narrator = score_result(
            _result(title="The Wise Man's Fear", narrator=""),
            _book(),
            prefer_narrator="Rupert Degas",
        )
        assert with_narrator > without_narrator

    def test_series_boosts_score(self):
        no_series = score_result(
            _result(title="The Wise Man's Fear"),
            _book(series=None),
        )
        with_series = score_result(
            _result(title="The Wise Man's Fear – Kingkiller Chronicle"),
            _book(series="The Kingkiller Chronicle"),
        )
        assert with_series > no_series

    def test_score_capped_at_100(self):
        score = score_result(
            _result(title="The Wise Man's Fear – Patrick Rothfuss", narrator="Rupert Degas"),
            _book(series="The Kingkiller Chronicle"),
            prefer_narrator="Rupert Degas",
        )
        assert score <= 100


class TestScoreAndSort:
    def test_filters_below_min_score(self):
        results = [
            _result(title="The Wise Man's Fear – Patrick Rothfuss"),
            _result(title="Completely Unrelated Audiobook", link="http://example.com/2"),
        ]
        scored = score_and_sort(results, _book(), min_score=60)
        assert all(s.score >= 60 for s in scored)

    def test_sorted_descending(self):
        results = [
            _result(title="Something Vaguely Similar", link="http://example.com/2"),
            _result(title="The Wise Man's Fear – Patrick Rothfuss"),
        ]
        scored = score_and_sort(results, _book(), min_score=0)
        if len(scored) >= 2:
            assert scored[0].score >= scored[1].score

    def test_filters_no_magnet(self):
        results = [
            _result(title="The Wise Man's Fear", magnet=""),
        ]
        scored = score_and_sort(results, _book(), min_score=0)
        assert len(scored) == 0

    def test_returns_scored_results(self):
        results = [_result()]
        scored = score_and_sort(results, _book(), min_score=0)
        assert len(scored) == 1
        assert scored[0].result == results[0]
        assert isinstance(scored[0].score, int)
