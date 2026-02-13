import json
from unittest.mock import MagicMock, patch

import pytest

from auto_torrent.types import SearchResult

# Sample API response from apibay.org/q.php
SAMPLE_RESULTS = [
    {
        "id": "11756968",
        "name": "Interstellar (2014) 1080p BrRip x264 - YIFY",
        "info_hash": "AB4F2ED0C1497B4FEBCB2B86902F1C4F2D7A4E9C",
        "leechers": "50",
        "seeders": "728",
        "size": "2431344640",
        "num_files": "2",
        "username": "YIFY",
        "added": "1420156800",
        "status": "vip",
        "category": "207",
        "imdb": "tt0816692",
    },
    {
        "id": "73959617",
        "name": "Interstellar.2014.1080p.BluRay.x265.10bit",
        "info_hash": "CD3E4F5A6B7C8D9E0F1A2B3C4D5E6F7A8B9C0D1E",
        "leechers": "30",
        "seeders": "141",
        "size": "4823449600",
        "num_files": "3",
        "username": "GalaxyRG",
        "added": "1630000000",
        "status": "trusted",
        "category": "207",
        "imdb": "tt0816692",
    },
]

NO_RESULTS = [{"id": "0", "name": "No results returned", "info_hash": "0000000000000000000000000000000000000000", "leechers": "0", "seeders": "0", "size": "0", "num_files": "0", "username": "", "added": "0", "status": "member", "category": "0", "imdb": ""}]


def _mock_response(data: list, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


class TestTPBSearch:
    @patch("auto_torrent.tpb.requests.get")
    def test_returns_search_results(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(SAMPLE_RESULTS)
        from auto_torrent.tpb import search

        results = search("interstellar")
        assert len(results) == 2
        assert all(isinstance(r, SearchResult) for r in results)

    @patch("auto_torrent.tpb.requests.get")
    def test_title_mapped(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(SAMPLE_RESULTS)
        from auto_torrent.tpb import search

        results = search("interstellar")
        assert results[0].title == "Interstellar (2014) 1080p BrRip x264 - YIFY"

    @patch("auto_torrent.tpb.requests.get")
    def test_magnet_built_from_info_hash(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(SAMPLE_RESULTS)
        from auto_torrent.tpb import search

        results = search("interstellar")
        assert results[0].magnet.startswith("magnet:?xt=urn:btih:AB4F2ED0C1497B4FEBCB2B86902F1C4F2D7A4E9C")
        assert "tr=" in results[0].magnet

    @patch("auto_torrent.tpb.requests.get")
    def test_file_size_human_readable(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(SAMPLE_RESULTS)
        from auto_torrent.tpb import search

        results = search("interstellar")
        assert "2.3" in results[0].file_size or "2.26" in results[0].file_size
        assert "GB" in results[0].file_size

    @patch("auto_torrent.tpb.requests.get")
    def test_link_points_to_tpb(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(SAMPLE_RESULTS)
        from auto_torrent.tpb import search

        results = search("interstellar")
        assert "11756968" in results[0].link

    @patch("auto_torrent.tpb.requests.get")
    def test_seeders_in_posted_field(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(SAMPLE_RESULTS)
        from auto_torrent.tpb import search

        results = search("interstellar")
        assert "728" in results[0].posted

    @patch("auto_torrent.tpb.requests.get")
    def test_no_results_returns_empty(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(NO_RESULTS)
        from auto_torrent.tpb import search

        results = search("asdfghjkl_nothing")
        assert results == []

    @patch("auto_torrent.tpb.requests.get")
    def test_connection_error_raises_tpb_error(self, mock_get: MagicMock) -> None:
        import requests as req

        mock_get.side_effect = req.ConnectionError("connection failed")
        from auto_torrent.tpb import TPBError, search

        with pytest.raises(TPBError, match="unreachable"):
            search("test")

    @patch("auto_torrent.tpb.requests.get")
    def test_timeout_raises_tpb_error(self, mock_get: MagicMock) -> None:
        import requests as req

        mock_get.side_effect = req.ConnectTimeout("timed out")
        from auto_torrent.tpb import TPBError, search

        with pytest.raises(TPBError, match="not responding"):
            search("test")

    @patch("auto_torrent.tpb.requests.get")
    def test_category_mapped(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(SAMPLE_RESULTS)
        from auto_torrent.tpb import search

        results = search("interstellar")
        assert results[0].category != ""


MIXED_CATEGORY_RESULTS = [
    {
        "id": "1", "name": "Movie.1080p", "info_hash": "A" * 40,
        "seeders": "100", "size": "2000000000", "category": "207",
        "leechers": "10", "num_files": "1", "username": "", "added": "0",
        "status": "member", "imdb": "",
    },
    {
        "id": "2", "name": "Game.v1.0", "info_hash": "B" * 40,
        "seeders": "50", "size": "5000000000", "category": "400",
        "leechers": "5", "num_files": "1", "username": "", "added": "0",
        "status": "member", "imdb": "",
    },
    {
        "id": "3", "name": "Song.mp3", "info_hash": "C" * 40,
        "seeders": "200", "size": "5000000", "category": "101",
        "leechers": "2", "num_files": "1", "username": "", "added": "0",
        "status": "member", "imdb": "",
    },
]

LOW_SEED_RESULTS = [
    {
        "id": "4", "name": "Rare.Movie.720p", "info_hash": "D" * 40,
        "seeders": "2", "size": "1000000000", "category": "207",
        "leechers": "1", "num_files": "1", "username": "", "added": "0",
        "status": "member", "imdb": "",
    },
    {
        "id": "5", "name": "Popular.Movie.1080p", "info_hash": "E" * 40,
        "seeders": "500", "size": "3000000000", "category": "207",
        "leechers": "20", "num_files": "1", "username": "", "added": "0",
        "status": "member", "imdb": "",
    },
]


class TestTPBCategoryFilter:
    @patch("auto_torrent.tpb.requests.get")
    def test_video_filter_excludes_non_video(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(MIXED_CATEGORY_RESULTS)
        from auto_torrent.tpb import search

        results = search("test", category="video")
        assert len(results) == 1
        assert results[0].title == "Movie.1080p"

    @patch("auto_torrent.tpb.requests.get")
    def test_audio_filter_returns_audio_only(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(MIXED_CATEGORY_RESULTS)
        from auto_torrent.tpb import search

        results = search("test", category="audio")
        assert len(results) == 1
        assert results[0].title == "Song.mp3"

    @patch("auto_torrent.tpb.requests.get")
    def test_all_category_returns_everything(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(MIXED_CATEGORY_RESULTS)
        from auto_torrent.tpb import search

        results = search("test", category="all")
        assert len(results) == 3

    @patch("auto_torrent.tpb.requests.get")
    def test_games_filter(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(MIXED_CATEGORY_RESULTS)
        from auto_torrent.tpb import search

        results = search("test", category="games")
        assert len(results) == 1
        assert results[0].title == "Game.v1.0"


class TestTPBMinSeeds:
    @patch("auto_torrent.tpb.requests.get")
    def test_default_min_seeds_filters_low(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(LOW_SEED_RESULTS)
        from auto_torrent.tpb import search

        results = search("test", category="all")
        assert len(results) == 1
        assert results[0].title == "Popular.Movie.1080p"

    @patch("auto_torrent.tpb.requests.get")
    def test_min_seeds_zero_returns_all(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(LOW_SEED_RESULTS)
        from auto_torrent.tpb import search

        results = search("test", category="all", min_seeds=0)
        assert len(results) == 2

    @patch("auto_torrent.tpb.requests.get")
    def test_min_seeds_custom_threshold(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(LOW_SEED_RESULTS)
        from auto_torrent.tpb import search

        results = search("test", category="all", min_seeds=100)
        assert len(results) == 1
        assert "500" in results[0].posted


class TestSizeWarning:
    def test_1080p_too_small_warns(self) -> None:
        from auto_torrent.tpb import check_size_warning

        warning = check_size_warning("Movie.1080p.BluRay", 100 * 1024**2)
        assert warning is not None
        assert "1080p" in warning

    def test_1080p_normal_size_no_warning(self) -> None:
        from auto_torrent.tpb import check_size_warning

        warning = check_size_warning("Movie.1080p.BluRay", 2 * 1024**3)
        assert warning is None

    def test_4k_too_small_warns(self) -> None:
        from auto_torrent.tpb import check_size_warning

        warning = check_size_warning("Movie.4K.HDR", 500 * 1024**2)
        assert warning is not None
        assert "4k" in warning.lower() or "4K" in warning

    def test_no_quality_tag_no_warning(self) -> None:
        from auto_torrent.tpb import check_size_warning

        warning = check_size_warning("Some Random Torrent", 10 * 1024**2)
        assert warning is None


class TestFileScan:
    def test_detects_exe_files(self, tmp_path) -> None:
        from auto_torrent.cli import _scan_for_suspicious_files

        (tmp_path / "movie.mkv").touch()
        (tmp_path / "setup.exe").touch()
        suspect = _scan_for_suspicious_files(str(tmp_path))
        assert len(suspect) == 1
        assert "setup.exe" in suspect[0]

    def test_clean_directory_returns_empty(self, tmp_path) -> None:
        from auto_torrent.cli import _scan_for_suspicious_files

        (tmp_path / "movie.mkv").touch()
        (tmp_path / "subtitles.srt").touch()
        suspect = _scan_for_suspicious_files(str(tmp_path))
        assert suspect == []

    def test_detects_nested_suspicious_files(self, tmp_path) -> None:
        from auto_torrent.cli import _scan_for_suspicious_files

        sub = tmp_path / "subfolder"
        sub.mkdir()
        (sub / "crack.bat").touch()
        suspect = _scan_for_suspicious_files(str(tmp_path))
        assert len(suspect) == 1
        assert "crack.bat" in suspect[0]

    def test_multiple_suspicious_types(self, tmp_path) -> None:
        from auto_torrent.cli import _scan_for_suspicious_files

        (tmp_path / "keygen.exe").touch()
        (tmp_path / "install.msi").touch()
        (tmp_path / "run.vbs").touch()
        (tmp_path / "movie.mp4").touch()
        suspect = _scan_for_suspicious_files(str(tmp_path))
        assert len(suspect) == 3
