import os
import time
from unittest.mock import patch

# Patch env + Twilio client before app module loads (module-level Settings())
_env = {
    "TWILIO_ACCOUNT_SID": "test",
    "TWILIO_AUTH_TOKEN": "test",
    "TWILIO_PHONE_NUMBER": "+10000000000",
    "ALLOWED_NUMBERS": '["+1234"]',
    "ABS_API_TOKEN": "test",
    "ABS_LIBRARY_ID": "test",
    "ATB_CWD": "/tmp",
}

with patch.dict(os.environ, _env), patch("auto_torrent.server.sms.Client"), patch("auto_torrent.server.sms.RequestValidator"):
    from auto_torrent.server.app import _try_quick_pick

from auto_torrent.server.llm import CONVERSATION_TTL, _conversations, store_pending_results, store_suggestions


def _make_results() -> list[dict]:
    return [
        {"title": "Book A", "magnet": "magnet:?xt=urn:btih:aaa", "score": 95},
        {"title": "Book B", "magnet": "magnet:?xt=urn:btih:bbb", "score": 90},
        {"title": "Book C", "magnet": "magnet:?xt=urn:btih:ccc", "score": 85},
    ]


class TestTryQuickPick:
    def setup_method(self):
        _conversations.clear()

    def test_digit_with_pending(self):
        store_pending_results("+1234", _make_results())
        result = _try_quick_pick("1", "+1234")
        assert result is not None
        assert result["title"] == "Book A"

    def test_second_digit(self):
        store_pending_results("+1234", _make_results())
        result = _try_quick_pick("2", "+1234")
        assert result is not None
        assert result["title"] == "Book B"

    def test_digit_without_pending(self):
        result = _try_quick_pick("1", "+1234")
        assert result is None

    def test_non_digit(self):
        store_pending_results("+1234", _make_results())
        result = _try_quick_pick("hello", "+1234")
        assert result is None

    def test_out_of_range(self):
        store_pending_results("+1234", _make_results())
        result = _try_quick_pick("9", "+1234")
        assert result is None

    def test_zero(self):
        store_pending_results("+1234", _make_results())
        result = _try_quick_pick("0", "+1234")
        assert result is None

    def test_negative(self):
        store_pending_results("+1234", _make_results())
        result = _try_quick_pick("-1", "+1234")
        assert result is None

    def test_float_not_matched(self):
        store_pending_results("+1234", _make_results())
        result = _try_quick_pick("1.5", "+1234")
        assert result is None

    def test_whitespace_around_digit(self):
        store_pending_results("+1234", _make_results())
        result = _try_quick_pick("  2  ", "+1234")
        assert result is not None
        assert result["title"] == "Book B"

    def test_text_with_number_not_matched(self):
        store_pending_results("+1234", _make_results())
        result = _try_quick_pick("the first one", "+1234")
        assert result is None

    def test_number_2_text_not_matched(self):
        store_pending_results("+1234", _make_results())
        result = _try_quick_pick("number 2", "+1234")
        assert result is None

    def test_expired_results_digit_returns_none(self):
        store_pending_results("+1234", _make_results())
        _conversations["+1234"]["ts"] = time.time() - CONVERSATION_TTL - 1
        result = _try_quick_pick("1", "+1234")
        assert result is None

    def test_suggestions_not_picked_by_digit(self):
        """Bare digits only match pending_results, not suggestions."""
        store_suggestions("+1234", ["Sug A", "Sug B"])
        result = _try_quick_pick("1", "+1234")
        assert result is None
