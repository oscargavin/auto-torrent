"""Tests for stream.py pure functions (no libtorrent dependency needed)."""

from unittest.mock import MagicMock, patch

import pytest


class TestDetectPlayer:
    @patch("auto_torrent.stream.shutil.which")
    def test_finds_mpv_first(self, mock_which: MagicMock) -> None:
        from auto_torrent.stream import _detect_player

        mock_which.side_effect = lambda name: "/usr/local/bin/mpv" if name == "mpv" else None
        assert _detect_player() == "mpv"

    @patch("auto_torrent.stream.shutil.which")
    def test_falls_back_to_iina(self, mock_which: MagicMock) -> None:
        from auto_torrent.stream import _detect_player

        def which_side(name: str) -> str | None:
            if name == "iina":
                return "/Applications/IINA.app/Contents/MacOS/iina"
            return None

        mock_which.side_effect = which_side
        assert _detect_player() == "iina"

    @patch("auto_torrent.stream.shutil.which")
    def test_falls_back_to_vlc(self, mock_which: MagicMock) -> None:
        from auto_torrent.stream import _detect_player

        def which_side(name: str) -> str | None:
            if name == "vlc":
                return "/usr/bin/vlc"
            return None

        mock_which.side_effect = which_side
        assert _detect_player() == "vlc"

    @patch("auto_torrent.stream.shutil.which")
    def test_returns_none_when_no_player(self, mock_which: MagicMock) -> None:
        from auto_torrent.stream import _detect_player

        mock_which.return_value = None
        assert _detect_player() is None


class TestSelectMediaFile:
    def _make_mock_torrent_info(
        self, files: list[tuple[str, int]]
    ) -> tuple[MagicMock, MagicMock]:
        mock_files = MagicMock()
        mock_files.num_files.return_value = len(files)
        mock_files.file_path.side_effect = lambda i: files[i][0]
        mock_files.file_size.side_effect = lambda i: files[i][1]

        mock_info = MagicMock()
        mock_info.files.return_value = mock_files

        mock_handle = MagicMock()

        return mock_info, mock_handle

    def test_picks_largest_media_file(self) -> None:
        from auto_torrent.stream import _select_media_file

        info, handle = self._make_mock_torrent_info([
            ("movie.mkv", 2_000_000_000),
            ("sample.mp4", 50_000_000),
            ("readme.txt", 1_000),
        ])
        idx, name, size = _select_media_file(info, handle)
        assert idx == 0
        assert name == "movie.mkv"
        assert size == 2_000_000_000

    def test_ignores_non_media(self) -> None:
        from auto_torrent.stream import _select_media_file

        info, handle = self._make_mock_torrent_info([
            ("readme.txt", 1_000),
            ("cover.jpg", 500_000),
            ("episode.avi", 700_000_000),
        ])
        idx, name, size = _select_media_file(info, handle)
        assert idx == 2
        assert name == "episode.avi"

    def test_raises_when_no_media(self) -> None:
        from auto_torrent.stream import StreamError, _select_media_file

        info, handle = self._make_mock_torrent_info([
            ("readme.txt", 1_000),
            ("cover.jpg", 500_000),
        ])
        with pytest.raises(StreamError, match="No media files"):
            _select_media_file(info, handle)

    def test_deprioritizes_other_files(self) -> None:
        from auto_torrent.stream import _select_media_file

        info, handle = self._make_mock_torrent_info([
            ("bonus.mp4", 100_000_000),
            ("movie.mkv", 2_000_000_000),
            ("subs.srt", 50_000),
        ])
        _select_media_file(info, handle)
        handle.prioritize_files.assert_called_once_with([0, 4, 0])


class TestFilePieceRange:
    def test_single_file_torrent(self) -> None:
        from auto_torrent.stream import _file_piece_range

        mock_info = MagicMock()
        mock_files = MagicMock()
        mock_files.file_offset.return_value = 0
        mock_files.file_size.return_value = 10_000_000
        mock_info.files.return_value = mock_files
        mock_info.piece_length.return_value = 262_144  # 256KB

        first, last = _file_piece_range(mock_info, 0)
        assert first == 0
        assert last == (10_000_000 - 1) // 262_144

    def test_file_at_offset(self) -> None:
        from auto_torrent.stream import _file_piece_range

        mock_info = MagicMock()
        mock_files = MagicMock()
        mock_files.file_offset.return_value = 1_000_000
        mock_files.file_size.return_value = 5_000_000
        mock_info.files.return_value = mock_files
        mock_info.piece_length.return_value = 262_144

        first, last = _file_piece_range(mock_info, 1)
        assert first == 1_000_000 // 262_144
        assert last == (1_000_000 + 5_000_000 - 1) // 262_144


class TestPrioritizeForStreaming:
    def test_first_pieces_get_priority_7(self) -> None:
        from auto_torrent.stream import _prioritize_for_streaming

        handle = MagicMock()
        mock_info = MagicMock()
        mock_info.num_pieces.return_value = 100
        handle.torrent_file.return_value = mock_info

        _prioritize_for_streaming(handle, first_piece=0, last_piece=99, buffer_pieces=10)

        priorities = handle.prioritize_pieces.call_args[0][0]
        # First 10 should be priority 7
        for i in range(10):
            assert priorities[i] == 7

        # Last 5 should be priority 7
        for i in range(95, 100):
            assert priorities[i] == 7

        # Middle should be priority 1
        assert priorities[50] == 1

    def test_small_torrent_all_prioritized(self) -> None:
        from auto_torrent.stream import _prioritize_for_streaming

        handle = MagicMock()
        mock_info = MagicMock()
        mock_info.num_pieces.return_value = 10
        handle.torrent_file.return_value = mock_info

        _prioritize_for_streaming(handle, first_piece=0, last_piece=9, buffer_pieces=20)

        priorities = handle.prioritize_pieces.call_args[0][0]
        # All pieces should be priority 7 (buffer covers entire torrent + last 5)
        for i in range(10):
            assert priorities[i] == 7


class TestFormatSpeed:
    def test_megabytes(self) -> None:
        from auto_torrent.stream import _format_speed

        assert _format_speed(5_242_880) == "5.0 MB/s"

    def test_kilobytes(self) -> None:
        from auto_torrent.stream import _format_speed

        assert _format_speed(51_200) == "50 KB/s"

    def test_bytes(self) -> None:
        from auto_torrent.stream import _format_speed

        assert _format_speed(500) == "500 B/s"


class TestMediaExtensions:
    def test_common_video_formats_included(self) -> None:
        from auto_torrent.stream import MEDIA_EXTENSIONS

        for ext in [".mkv", ".mp4", ".avi", ".mov", ".webm"]:
            assert ext in MEDIA_EXTENSIONS

    def test_non_media_excluded(self) -> None:
        from auto_torrent.stream import MEDIA_EXTENSIONS

        for ext in [".txt", ".jpg", ".srt", ".nfo", ".exe"]:
            assert ext not in MEDIA_EXTENSIONS


class TestStreamError:
    def test_is_exception(self) -> None:
        from auto_torrent.stream import StreamError

        with pytest.raises(StreamError):
            raise StreamError("test")


class TestCleanup:
    def test_removes_directory_when_not_keep(self, tmp_path) -> None:
        from auto_torrent.stream import _cleanup

        save_path = tmp_path / "stream_test"
        save_path.mkdir()
        (save_path / "movie.mkv").touch()

        _cleanup(None, None, None, save_path, keep=False, log=lambda *a, **k: None)
        assert not save_path.exists()

    def test_keeps_directory_when_keep(self, tmp_path) -> None:
        from auto_torrent.stream import _cleanup

        save_path = tmp_path / "stream_test"
        save_path.mkdir()
        (save_path / "movie.mkv").touch()

        _cleanup(None, None, None, save_path, keep=True, log=lambda *a, **k: None)
        assert save_path.exists()

    def test_shuts_down_server(self) -> None:
        from auto_torrent.stream import _cleanup

        server = MagicMock()
        _cleanup(server, None, None, None, keep=False, log=lambda *a, **k: None)
        server.shutdown.assert_called_once()

    def test_removes_torrent_from_session(self) -> None:
        from auto_torrent.stream import _cleanup

        session = MagicMock()
        handle = MagicMock()
        _cleanup(None, session, handle, None, keep=False, log=lambda *a, **k: None)
        session.remove_torrent.assert_called_once_with(handle)
