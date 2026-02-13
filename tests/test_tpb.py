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
