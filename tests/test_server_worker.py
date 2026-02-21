from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auto_torrent.server.worker import (
    SearchDecision,
    decide_action,
    download_audiobook,
    format_results_sms,
    search_audiobook,
)


class TestDecideAction:
    def test_no_results(self):
        decision = decide_action([])
        assert not decision.auto_download
        assert decision.chosen_index is None

    def test_single_result_above_threshold(self):
        decision = decide_action([{"score": 80}])
        assert decision.auto_download
        assert decision.chosen_index == 0

    def test_single_result_below_threshold(self):
        decision = decide_action([{"score": 60}])
        assert not decision.auto_download

    def test_clear_winner_high_gap(self):
        decision = decide_action([{"score": 95}, {"score": 70}])
        assert decision.auto_download
        assert decision.chosen_index == 0

    def test_ambiguous_small_gap(self):
        decision = decide_action([{"score": 95}, {"score": 90}])
        assert not decision.auto_download

    def test_at_threshold_boundary_with_gap(self):
        decision = decide_action([{"score": 85}, {"score": 70}])
        assert decision.auto_download
        assert decision.chosen_index == 0

    def test_at_threshold_gap_too_small(self):
        decision = decide_action([{"score": 85}, {"score": 75}])
        assert not decision.auto_download

    def test_missing_score_treated_as_zero(self):
        decision = decide_action([{}, {"score": 50}])
        assert not decision.auto_download

    def test_single_result_at_exact_threshold(self):
        decision = decide_action([{"score": 75}])
        assert decision.auto_download

    def test_custom_threshold(self):
        decision = decide_action([{"score": 70}], threshold=70, gap=10)
        assert decision.auto_download


class TestFormatResultsSms:
    def test_single_result_with_narrator(self):
        results = [{"title": "The Wise Man's Fear", "narrator": "Rupert Degas", "format": "M4B", "score": 95}]
        text = format_results_sms(results)
        assert "1." in text
        assert "Rupert Degas" in text
        assert "M4B" in text
        assert "95%" in text

    def test_multiple_results_numbered(self):
        results = [
            {"title": "Book A", "narrator": "Narrator A", "format": "M4B", "score": 95},
            {"title": "Book B", "narrator": "Narrator B", "format": "MP3", "score": 90},
        ]
        text = format_results_sms(results)
        assert "1." in text
        assert "2." in text
        assert "Book A" in text
        assert "Book B" in text

    def test_max_results_respected(self):
        results = [{"title": f"Book {i}", "score": 90 - i} for i in range(5)]
        text = format_results_sms(results, max_results=3)
        assert "3." in text
        assert "4." not in text

    def test_missing_narrator_shows_author(self):
        results = [{"title": "Some Book", "author": "Jane Doe", "score": 80}]
        text = format_results_sms(results)
        assert "Jane Doe" in text

    def test_missing_format_omitted(self):
        results = [{"title": "Some Book", "narrator": "Someone", "score": 80}]
        text = format_results_sms(results)
        assert "Some Book" in text
        assert "Someone" in text

    def test_empty_results_fallback(self):
        text = format_results_sms([])
        assert text  # should return a non-empty fallback

    def test_reply_prompt_included(self):
        results = [{"title": "Book A", "score": 90}]
        text = format_results_sms(results)
        assert "reply" in text.lower() or "number" in text.lower()


class TestSearchAudiobook:
    @pytest.mark.anyio
    async def test_calls_atb_without_auto_or_bg(self):
        mock_settings = MagicMock()
        mock_settings.atb_cwd = "/tmp"
        with patch("auto_torrent.server.worker._run_atb") as mock_run:
            mock_run.return_value = {"results": [{"title": "Book A", "score": 95}]}
            result = await search_audiobook("test query", mock_settings)

            args = mock_run.call_args[0][0]
            assert "--auto" not in args
            assert "--bg" not in args
            assert "search" in args
            assert "test query" in args

    @pytest.mark.anyio
    async def test_returns_none_on_error(self):
        mock_settings = MagicMock()
        mock_settings.atb_cwd = "/tmp"
        with patch("auto_torrent.server.worker._run_atb") as mock_run:
            mock_run.return_value = None
            result = await search_audiobook("test", mock_settings)
            assert result is None

    @pytest.mark.anyio
    async def test_returns_none_on_error_key(self):
        mock_settings = MagicMock()
        mock_settings.atb_cwd = "/tmp"
        with patch("auto_torrent.server.worker._run_atb") as mock_run:
            mock_run.return_value = {"error": "something broke"}
            result = await search_audiobook("test", mock_settings)
            assert result is None


class TestDownloadAudiobook:
    @pytest.mark.anyio
    async def test_calls_atb_download_with_bg(self):
        mock_settings = MagicMock()
        mock_settings.atb_cwd = "/tmp"
        with patch("auto_torrent.server.worker._run_atb") as mock_run:
            mock_run.return_value = {
                "download": {"id": "abc", "path": "/tmp/dl"},
            }
            result = await download_audiobook("magnet:?xt=urn:btih:abc", "Title", "Author", mock_settings)

            args = mock_run.call_args[0][0]
            assert "download" in args
            assert "--bg" in args
            assert "--title" in args
            assert result is not None
            assert result["download"]["id"] == "abc"

    @pytest.mark.anyio
    async def test_returns_none_on_failure(self):
        mock_settings = MagicMock()
        mock_settings.atb_cwd = "/tmp"
        with patch("auto_torrent.server.worker._run_atb") as mock_run:
            mock_run.return_value = None
            result = await download_audiobook("magnet:?x", "T", "A", mock_settings)
            assert result is None
