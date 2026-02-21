import time

from auto_torrent.server.llm import (
    _conversations,
    clear_conversation,
    get_pending_result,
    has_pending_results,
    store_pending_results,
    store_suggestions,
    CONVERSATION_TTL,
)


def _make_results() -> list[dict]:
    return [
        {"title": "Book A", "magnet": "magnet:?xt=urn:btih:aaa", "score": 95},
        {"title": "Book B", "magnet": "magnet:?xt=urn:btih:bbb", "score": 90},
        {"title": "Book C", "magnet": "magnet:?xt=urn:btih:ccc", "score": 85},
    ]


class TestStorePendingResults:
    def setup_method(self):
        _conversations.clear()

    def test_stores_results(self):
        store_pending_results("+1234", _make_results())
        assert has_pending_results("+1234")

    def test_clears_existing_suggestions(self):
        store_suggestions("+1234", ["Sug A", "Sug B"])
        store_pending_results("+1234", _make_results())
        assert "suggestions" not in _conversations["+1234"] or not _conversations["+1234"].get("suggestions")

    def test_results_and_suggestions_mutually_exclusive(self):
        store_pending_results("+1234", _make_results())
        store_suggestions("+1234", ["Sug A"])
        assert not has_pending_results("+1234")


class TestGetPendingResult:
    def setup_method(self):
        _conversations.clear()

    def test_returns_by_1based_index(self):
        store_pending_results("+1234", _make_results())
        result = get_pending_result("+1234", 1)
        assert result is not None
        assert result["title"] == "Book A"

    def test_returns_second(self):
        store_pending_results("+1234", _make_results())
        result = get_pending_result("+1234", 2)
        assert result is not None
        assert result["title"] == "Book B"

    def test_index_zero_returns_none(self):
        store_pending_results("+1234", _make_results())
        assert get_pending_result("+1234", 0) is None

    def test_negative_index_returns_none(self):
        store_pending_results("+1234", _make_results())
        assert get_pending_result("+1234", -1) is None

    def test_out_of_range_returns_none(self):
        store_pending_results("+1234", _make_results())
        assert get_pending_result("+1234", 10) is None

    def test_expired_returns_none(self):
        store_pending_results("+1234", _make_results())
        _conversations["+1234"]["ts"] = time.time() - CONVERSATION_TTL - 1
        assert get_pending_result("+1234", 1) is None

    def test_no_phone_returns_none(self):
        assert get_pending_result("+9999", 1) is None


class TestHasPendingResults:
    def setup_method(self):
        _conversations.clear()

    def test_true_when_present(self):
        store_pending_results("+1234", _make_results())
        assert has_pending_results("+1234") is True

    def test_false_when_empty(self):
        assert has_pending_results("+1234") is False

    def test_false_when_expired(self):
        store_pending_results("+1234", _make_results())
        _conversations["+1234"]["ts"] = time.time() - CONVERSATION_TTL - 1
        assert has_pending_results("+1234") is False


class TestClearConversation:
    def setup_method(self):
        _conversations.clear()

    def test_removes_all_state(self):
        store_pending_results("+1234", _make_results())
        clear_conversation("+1234")
        assert "+1234" not in _conversations

    def test_noop_on_missing_phone(self):
        clear_conversation("+9999")  # should not raise


class TestEdgeCases:
    def setup_method(self):
        _conversations.clear()

    def test_expired_results_digit_falls_through(self):
        """Expired pending results + digit → None (falls through to LLM)."""
        store_pending_results("+1234", _make_results())
        _conversations["+1234"]["ts"] = time.time() - CONVERSATION_TTL - 1
        assert get_pending_result("+1234", 1) is None

    def test_new_search_clears_old_pending(self):
        """Storing new pending results replaces old ones."""
        store_pending_results("+1234", _make_results())
        new_results = [{"title": "New Book", "magnet": "magnet:?xt=urn:btih:new", "score": 80}]
        store_pending_results("+1234", new_results)
        result = get_pending_result("+1234", 1)
        assert result is not None
        assert result["title"] == "New Book"
        assert get_pending_result("+1234", 2) is None  # old results gone

    def test_suggestions_then_results_clears_suggestions(self):
        """Storing search results clears existing suggestions."""
        store_suggestions("+1234", ["Sug A", "Sug B"])
        store_pending_results("+1234", _make_results())
        conv = _conversations["+1234"]
        assert not conv.get("suggestions")
        assert conv.get("pending_results")

    def test_results_then_suggestions_clears_results(self):
        """Storing suggestions clears existing pending results."""
        store_pending_results("+1234", _make_results())
        store_suggestions("+1234", ["Sug A"])
        assert not has_pending_results("+1234")

    def test_context_includes_pending_results_info(self):
        """_get_context should not leak pending_results into LLM prompt."""
        from auto_torrent.server.llm import _get_context
        store_pending_results("+1234", _make_results())
        context = _get_context("+1234")
        # pending_results are not suggestions — context should be empty
        assert "pending_suggestions" not in context
