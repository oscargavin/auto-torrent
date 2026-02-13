from unittest.mock import MagicMock, patch

import pytest

from auto_torrent.tpb import TPBError, TPBResult, _check_size_warning, parse_title, score_result, search

# Minimal fields needed by apibay API — tests only include what search() reads
SAMPLE_RESULTS = [
    {
        "id": "11756968",
        "name": "Interstellar (2014) 1080p BrRip x264 - YIFY",
        "info_hash": "AB4F2ED0C1497B4FEBCB2B86902F1C4F2D7A4E9C",
        "seeders": "728",
        "size": "2431344640",
        "category": "207",
        "status": "vip",
    },
    {
        "id": "73959617",
        "name": "Interstellar.2014.1080p.BluRay.x265.10bit",
        "info_hash": "CD3E4F5A6B7C8D9E0F1A2B3C4D5E6F7A8B9C0D1E",
        "seeders": "141",
        "size": "4823449600",
        "category": "207",
        "status": "trusted",
    },
]

NO_RESULTS = [{"id": "0", "name": "No results returned", "info_hash": "0" * 40, "seeders": "0", "size": "0", "category": "0"}]

MIXED_CATEGORY_RESULTS = [
    {"id": "1", "name": "Movie.1080p", "info_hash": "A" * 40, "seeders": "100", "size": "2000000000", "category": "207"},
    {"id": "2", "name": "Game.v1.0", "info_hash": "B" * 40, "seeders": "50", "size": "5000000000", "category": "400"},
    {"id": "3", "name": "Song.mp3", "info_hash": "C" * 40, "seeders": "200", "size": "5000000", "category": "101"},
]

LOW_SEED_RESULTS = [
    {"id": "4", "name": "Rare.Movie.720p", "info_hash": "D" * 40, "seeders": "2", "size": "1000000000", "category": "207"},
    {"id": "5", "name": "Popular.Movie.1080p", "info_hash": "E" * 40, "seeders": "500", "size": "3000000000", "category": "207"},
]


def _mock_response(data: list) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


class TestTPBSearch:
    @patch("auto_torrent.tpb.requests.get")
    def test_returns_search_results(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(SAMPLE_RESULTS)
        results = search("interstellar")
        assert len(results) == 2
        assert all(isinstance(r, TPBResult) for r in results)

    @patch("auto_torrent.tpb.requests.get")
    def test_title_mapped(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(SAMPLE_RESULTS)
        results = search("interstellar")
        assert results[0].title == "Interstellar (2014) 1080p BrRip x264 - YIFY"

    @patch("auto_torrent.tpb.requests.get")
    def test_magnet_built_from_info_hash(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(SAMPLE_RESULTS)
        results = search("interstellar")
        assert results[0].magnet.startswith("magnet:?xt=urn:btih:AB4F2ED0C1497B4FEBCB2B86902F1C4F2D7A4E9C")
        assert "tr=" in results[0].magnet

    @patch("auto_torrent.tpb.requests.get")
    def test_file_size_human_readable(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(SAMPLE_RESULTS)
        results = search("interstellar")
        assert "GB" in results[0].file_size

    @patch("auto_torrent.tpb.requests.get")
    def test_size_bytes_preserved(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(SAMPLE_RESULTS)
        results = search("interstellar")
        assert results[0].size_bytes == 2431344640

    @patch("auto_torrent.tpb.requests.get")
    def test_link_points_to_tpb(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(SAMPLE_RESULTS)
        results = search("interstellar")
        assert "11756968" in results[0].link

    @patch("auto_torrent.tpb.requests.get")
    def test_seeders_as_int(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(SAMPLE_RESULTS)
        results = search("interstellar")
        assert results[0].seeders == 728

    @patch("auto_torrent.tpb.requests.get")
    def test_no_results_returns_empty(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(NO_RESULTS)
        assert search("asdfghjkl_nothing") == []

    @patch("auto_torrent.tpb.requests.get")
    def test_connection_error_raises_tpb_error(self, mock_get: MagicMock) -> None:
        import requests as req
        mock_get.side_effect = req.ConnectionError("connection failed")
        with pytest.raises(TPBError, match="unreachable"):
            search("test")

    @patch("auto_torrent.tpb.requests.get")
    def test_timeout_raises_tpb_error(self, mock_get: MagicMock) -> None:
        import requests as req
        mock_get.side_effect = req.ConnectTimeout("timed out")
        with pytest.raises(TPBError, match="not responding"):
            search("test")

    @patch("auto_torrent.tpb.requests.get")
    def test_category_mapped(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(SAMPLE_RESULTS)
        results = search("interstellar")
        assert results[0].category == "HD Movies"


class TestTPBCategoryFilter:
    @patch("auto_torrent.tpb.requests.get")
    def test_video_filter_excludes_non_video(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(MIXED_CATEGORY_RESULTS)
        results = search("test", category="video")
        assert len(results) == 1
        assert results[0].title == "Movie.1080p"

    @patch("auto_torrent.tpb.requests.get")
    def test_audio_filter_returns_audio_only(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(MIXED_CATEGORY_RESULTS)
        results = search("test", category="audio")
        assert len(results) == 1
        assert results[0].title == "Song.mp3"

    @patch("auto_torrent.tpb.requests.get")
    def test_all_category_returns_everything(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(MIXED_CATEGORY_RESULTS)
        results = search("test", category="all")
        assert len(results) == 3

    @patch("auto_torrent.tpb.requests.get")
    def test_games_filter(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(MIXED_CATEGORY_RESULTS)
        results = search("test", category="games")
        assert len(results) == 1
        assert results[0].title == "Game.v1.0"


class TestTPBMinSeeds:
    @patch("auto_torrent.tpb.requests.get")
    def test_default_min_seeds_filters_low(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(LOW_SEED_RESULTS)
        results = search("test", category="all")
        assert len(results) == 1
        assert results[0].title == "Popular.Movie.1080p"

    @patch("auto_torrent.tpb.requests.get")
    def test_min_seeds_zero_returns_all(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(LOW_SEED_RESULTS)
        results = search("test", category="all", min_seeds=0)
        assert len(results) == 2

    @patch("auto_torrent.tpb.requests.get")
    def test_min_seeds_custom_threshold(self, mock_get: MagicMock) -> None:
        mock_get.return_value = _mock_response(LOW_SEED_RESULTS)
        results = search("test", category="all", min_seeds=100)
        assert len(results) == 1
        assert results[0].seeders == 500


class TestSizeWarning:
    def test_1080p_too_small_warns(self) -> None:
        assert _check_size_warning("Movie.1080p.BluRay", 100 * 1024**2) is not None

    def test_1080p_normal_size_no_warning(self) -> None:
        assert _check_size_warning("Movie.1080p.BluRay", 2 * 1024**3) is None

    def test_4k_too_small_warns(self) -> None:
        warning = _check_size_warning("Movie.4K.HDR", 500 * 1024**2)
        assert warning is not None

    def test_no_quality_tag_no_warning(self) -> None:
        assert _check_size_warning("Some Random Torrent", 10 * 1024**2) is None

    @patch("auto_torrent.tpb.requests.get")
    def test_warning_attached_to_result(self, mock_get: MagicMock) -> None:
        fake = [{"id": "1", "name": "Movie.1080p", "info_hash": "A" * 40, "seeders": "100", "size": "5000000", "category": "207"}]
        mock_get.return_value = _mock_response(fake)
        results = search("test", category="all")
        assert results[0].warning is not None
        assert "1080p" in results[0].warning


class TestParseTitle:
    def test_parses_resolution(self) -> None:
        info = parse_title("Movie.2014.1080p.BluRay.x265")
        assert info["resolution"] == "1080p"

    def test_parses_4k(self) -> None:
        info = parse_title("Movie.2160p.UHD.BluRay")
        assert info["resolution"] == "2160p"

    def test_parses_720p(self) -> None:
        info = parse_title("Movie.720p.WEB-DL")
        assert info["resolution"] == "720p"

    def test_parses_source_bluray(self) -> None:
        info = parse_title("Movie.1080p.BluRay.x264")
        assert info["source"] == "bluray"

    def test_parses_source_webdl(self) -> None:
        info = parse_title("Movie.1080p.WEB-DL.DD5.1")
        assert info["source"] == "web-dl"

    def test_parses_source_webrip(self) -> None:
        info = parse_title("Movie.1080p.WEBRip.x264")
        assert info["source"] == "webrip"

    def test_parses_source_cam(self) -> None:
        info = parse_title("Movie.2024.CAM.x264")
        assert info["source"] == "cam"

    def test_parses_codec_x265(self) -> None:
        info = parse_title("Movie.1080p.BluRay.x265.10bit")
        assert info["codec"] == "x265"

    def test_parses_codec_x264(self) -> None:
        info = parse_title("Movie.1080p.BluRay.x264-YIFY")
        assert info["codec"] == "x264"

    def test_parses_hevc_as_x265(self) -> None:
        info = parse_title("Movie.1080p.HEVC.BluRay")
        assert info["codec"] == "x265"

    def test_parses_hdr(self) -> None:
        info = parse_title("Movie.2160p.BluRay.x265.HDR")
        assert info["hdr"] is True

    def test_parses_dolby_vision(self) -> None:
        info = parse_title("Movie.2160p.BluRay.DV.x265")
        assert info["hdr"] is True

    def test_no_hdr_when_absent(self) -> None:
        info = parse_title("Movie.1080p.BluRay.x264")
        assert info["hdr"] is False

    def test_unknown_resolution(self) -> None:
        info = parse_title("Some.Random.Torrent")
        assert info["resolution"] is None

    def test_unknown_source(self) -> None:
        info = parse_title("Movie.1080p.x264")
        assert info["source"] is None


class TestScoreResult:
    def test_bluray_1080p_high_seeds_scores_high(self) -> None:
        r = TPBResult(
            title="Movie.1080p.BluRay.x265", link="", magnet="",
            file_size="2.3 GB", size_bytes=2431344640, seeders=500,
            category="HD Movies", status="vip",
        )
        assert score_result(r) >= 80

    def test_cam_scores_low(self) -> None:
        r = TPBResult(
            title="Movie.CAM.x264", link="", magnet="",
            file_size="1.2 GB", size_bytes=1200000000, seeders=50,
            category="Movies", status="member",
        )
        assert score_result(r) < 50

    def test_more_seeders_scores_higher(self) -> None:
        base = dict(title="Movie.1080p.BluRay.x264", link="", magnet="",
                    file_size="2 GB", size_bytes=2*1024**3, category="HD Movies", status="member")
        low = TPBResult(**base, seeders=10)
        high = TPBResult(**base, seeders=1000)
        assert score_result(high) > score_result(low)

    def test_vip_scores_higher_than_member(self) -> None:
        base = dict(title="Movie.1080p.BluRay.x264", link="", magnet="",
                    file_size="2 GB", size_bytes=2*1024**3, seeders=100, category="HD Movies")
        vip = TPBResult(**base, status="vip")
        member = TPBResult(**base, status="member")
        assert score_result(vip) > score_result(member)

    def test_x265_scores_higher_than_x264(self) -> None:
        base = dict(link="", magnet="", file_size="2 GB",
                    size_bytes=2*1024**3, seeders=100, category="HD Movies", status="member")
        h265 = TPBResult(title="Movie.1080p.BluRay.x265", **base)
        h264 = TPBResult(title="Movie.1080p.BluRay.x264", **base)
        assert score_result(h265) > score_result(h264)

    def test_bluray_beats_webrip(self) -> None:
        base = dict(link="", magnet="", file_size="2 GB",
                    size_bytes=2*1024**3, seeders=100, category="HD Movies", status="member")
        bluray = TPBResult(title="Movie.1080p.BluRay.x264", **base)
        webrip = TPBResult(title="Movie.1080p.WEBRip.x264", **base)
        assert score_result(bluray) > score_result(webrip)

    def test_hdr_bonus_for_4k(self) -> None:
        base = dict(link="", magnet="", file_size="20 GB",
                    size_bytes=20*1024**3, seeders=100, category="UHD Movies", status="member")
        hdr = TPBResult(title="Movie.2160p.BluRay.x265.HDR", **base)
        sdr = TPBResult(title="Movie.2160p.BluRay.x265", **base)
        assert score_result(hdr) > score_result(sdr)

    def test_score_capped_at_100(self) -> None:
        r = TPBResult(
            title="Movie.2160p.BluRay.x265.HDR.DV", link="", magnet="",
            file_size="50 GB", size_bytes=50*1024**3, seeders=10000,
            category="UHD Movies", status="vip",
        )
        assert score_result(r) <= 100

    @patch("auto_torrent.tpb.requests.get")
    def test_search_returns_sorted_by_score(self, mock_get: MagicMock) -> None:
        data = [
            {"id": "1", "name": "Movie.CAM", "info_hash": "A" * 40, "seeders": "50", "size": "700000000", "category": "201", "status": "member"},
            {"id": "2", "name": "Movie.1080p.BluRay.x265", "info_hash": "B" * 40, "seeders": "500", "size": "3000000000", "category": "207", "status": "vip"},
            {"id": "3", "name": "Movie.720p.WEBRip", "info_hash": "C" * 40, "seeders": "100", "size": "1000000000", "category": "207", "status": "trusted"},
        ]
        mock_get.return_value = _mock_response(data)
        results = search("test", category="all")
        assert results[0].title == "Movie.1080p.BluRay.x265"
        assert results[-1].title == "Movie.CAM"


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
        assert _scan_for_suspicious_files(str(tmp_path)) == []

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
        assert len(_scan_for_suspicious_files(str(tmp_path))) == 3
