"""Stream torrents via libtorrent + local HTTP server."""

import mimetypes
import shutil
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import libtorrent as lt

from .config import DEFAULT_TRACKERS, STREAM_BUFFER_MB, STREAM_DIR, STREAM_PORT

MEDIA_EXTENSIONS = frozenset({
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts", ".vob",
})

DHT_BOOTSTRAP_NODES = [
    ("router.bittorrent.com", 6881),
    ("router.utorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
]


class StreamError(Exception):
    pass



def _create_session() -> lt.session:
    settings = {
        "listen_interfaces": "0.0.0.0:6881",
        "enable_dht": True,
        "enable_lsd": True,
        "enable_upnp": True,
        "enable_natpmp": True,
    }
    ses = lt.session(settings)
    for host, port in DHT_BOOTSTRAP_NODES:
        ses.add_dht_router(host, port)
    return ses


def _add_magnet(
    session: lt.session,
    magnet: str,
    save_path: Path,
    trackers: list[str],
) -> lt.torrent_handle:
    params = lt.parse_magnet_uri(magnet)
    params.save_path = str(save_path)
    params.flags |= lt.torrent_flags.sequential_download

    for tracker in trackers:
        params.trackers.append(tracker)

    return session.add_torrent(params)



def _wait_for_metadata(
    session: lt.session,
    handle: lt.torrent_handle,
    timeout: int = 120,
    log: Callable[..., None] = print,
) -> lt.torrent_info:
    deadline = time.monotonic() + timeout
    while not handle.has_metadata():
        session.post_torrent_updates()
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            raise StreamError(
                f"No metadata received after {timeout}s — "
                "check your connection or try a torrent with more seeders"
            )
        log(f"\r  Waiting for metadata... {remaining}s remaining", end="", flush=True)
        time.sleep(1)
    log("\r" + " " * 60 + "\r", end="", flush=True)
    return handle.torrent_file()


def _select_media_file(
    torrent_info: lt.torrent_info,
    handle: lt.torrent_handle,
) -> tuple[int, str, int]:
    files = torrent_info.files()
    best_idx = -1
    best_size = 0
    best_name = ""

    for i in range(files.num_files()):
        path = files.file_path(i)
        ext = Path(path).suffix.lower()
        size = files.file_size(i)
        if ext in MEDIA_EXTENSIONS and size > best_size:
            best_idx = i
            best_size = size
            best_name = Path(path).name

    if best_idx < 0:
        all_files = [files.file_path(i) for i in range(files.num_files())]
        raise StreamError(
            f"No media files found in torrent. Files: {', '.join(all_files)}"
        )

    priorities = [0] * files.num_files()
    priorities[best_idx] = 4
    handle.prioritize_files(priorities)

    return best_idx, best_name, best_size



def _file_piece_range(
    torrent_info: lt.torrent_info,
    file_index: int,
) -> tuple[int, int]:
    files = torrent_info.files()
    file_offset = files.file_offset(file_index)
    file_size = files.file_size(file_index)
    piece_length = torrent_info.piece_length()

    first_piece = file_offset // piece_length
    last_piece = (file_offset + file_size - 1) // piece_length

    return first_piece, last_piece


def _prioritize_for_streaming(
    handle: lt.torrent_handle,
    first_piece: int,
    last_piece: int,
    buffer_pieces: int = 20,
) -> None:
    """Set high priority on first N and last 5 pieces (MP4 moov atom)."""
    num_pieces = handle.torrent_file().num_pieces()
    priorities = [1] * num_pieces

    for i in range(first_piece, min(first_piece + buffer_pieces, last_piece + 1)):
        priorities[i] = 7

    # Last 5 pieces for MP4 moov atom
    tail_start = max(last_piece - 4, first_piece)
    for i in range(tail_start, last_piece + 1):
        priorities[i] = 7

    handle.prioritize_pieces(priorities)



def _make_handler(
    file_path: Path,
    file_size: int,
    torrent_info: lt.torrent_info,
    handle: lt.torrent_handle,
    file_index: int,
) -> type[BaseHTTPRequestHandler]:
    first_piece, last_piece = _file_piece_range(torrent_info, file_index)
    piece_length = torrent_info.piece_length()
    files = torrent_info.files()
    file_offset = files.file_offset(file_index)
    mime_type = mimetypes.guess_type(str(file_path))[0] or "video/mp4"

    class _StreamHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _wait_for_pieces(self, start_byte: int, end_byte: int) -> bool:
            needed_first = (file_offset + start_byte) // piece_length
            needed_last = (file_offset + end_byte) // piece_length

            for p in range(needed_first, min(needed_last + 20, last_piece + 1)):
                handle.piece_priority(p, 7)

            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status = handle.status()
                pieces = status.pieces
                all_ready = True
                for p in range(needed_first, needed_last + 1):
                    if not pieces[p]:
                        all_ready = False
                        break
                if all_ready:
                    return True
                time.sleep(0.2)
            return False

        def _serve_file(self, head_only: bool = False) -> None:
            range_header = self.headers.get("Range")

            if range_header:
                try:
                    range_spec = range_header.replace("bytes=", "")
                    start_str, end_str = range_spec.split("-")
                    start = int(start_str)
                    end = int(end_str) if end_str else file_size - 1
                except (ValueError, IndexError):
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return

                end = min(end, file_size - 1)

                if not self._wait_for_pieces(start, end):
                    self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Buffering timeout")
                    return

                length = end - start + 1
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
            else:
                if not self._wait_for_pieces(0, min(piece_length * 5, file_size - 1)):
                    self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Buffering timeout")
                    return

                start = 0
                length = file_size
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Length", str(file_size))

            self.send_header("Content-Type", mime_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            if head_only:
                return

            try:
                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    chunk = 64 * 1024
                    while remaining > 0:
                        data = f.read(min(chunk, remaining))
                        if not data:
                            break
                        self.wfile.write(data)
                        remaining -= len(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self) -> None:
            self._serve_file()

        def do_HEAD(self) -> None:
            self._serve_file(head_only=True)

    return _StreamHandler


class _QuietHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        pass


def _start_http_server(
    port: int,
    handler_class: type[BaseHTTPRequestHandler],
) -> ThreadingHTTPServer:
    try:
        server = _QuietHTTPServer(("127.0.0.1", port), handler_class)
    except OSError as e:
        if "Address already in use" in str(e) or e.errno == 48:
            raise StreamError(
                f"Port {port} is already in use — try --port with a different number"
            )
        raise

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server



PLAYER_CANDIDATES = ["mpv", "iina", "vlc"]


def _detect_player() -> str | None:
    for name in PLAYER_CANDIDATES:
        if shutil.which(name):
            return name
    return None


def _launch_player(player: str, url: str) -> subprocess.Popen | None:
    args: list[str]

    if player == "mpv":
        args = [player, "--cache=yes", "--demuxer-max-bytes=50M", url]
    elif player == "iina":
        # iina-cli has a known bug parsing HTTP URLs (shows auth dialog).
        # Use macOS `open` with iina:// URL scheme instead — fire-and-forget.
        iina_url = f"iina://weblink?url={quote(url, safe='')}"
        subprocess.run(["open", iina_url], check=False)
        return None
    elif player == "vlc":
        args = ["vlc", "--network-caching=5000", url]
    else:
        args = [player, url]

    try:
        return subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        return None



def _format_speed(bps: int) -> str:
    if bps >= 1024**2:
        return f"{bps / 1024**2:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps} B/s"


def _print_status(status: lt.torrent_status, log: Callable[..., None]) -> None:
    pieces = status.pieces
    have = sum(1 for p in pieces if p)
    buffered = have / len(pieces) * 100 if pieces else 0

    log(
        f"\r  Streaming: {status.progress * 100:.0f}% | {_format_speed(status.download_rate)} | "
        f"{status.num_peers} peers | buffered: {buffered:.0f}%",
        end="", flush=True,
    )



def _cleanup(
    server: ThreadingHTTPServer | None,
    session: lt.session | None,
    handle: lt.torrent_handle | None,
    save_path: Path | None,
    keep: bool,
    log: Callable[..., None] = print,
) -> None:
    if server:
        server.shutdown()

    if session and handle:
        session.remove_torrent(handle)

    if not keep and save_path and save_path.exists():
        try:
            shutil.rmtree(save_path)
            log(f"\n  Cleaned up: {save_path}")
        except OSError:
            pass



def stream(
    magnet: str,
    player: str = "auto",
    port: int = STREAM_PORT,
    save_path: Path | None = None,
    trackers: list[str] | None = None,
    keep: bool = False,
    json_mode: bool = False,
    log: Callable[..., None] = print,
) -> dict:
    if save_path is None:
        save_path = STREAM_DIR
    save_path.mkdir(parents=True, exist_ok=True)

    if trackers is None:
        trackers = list(DEFAULT_TRACKERS)

    session: lt.session | None = None
    handle: lt.torrent_handle | None = None
    server: ThreadingHTTPServer | None = None
    player_proc: subprocess.Popen | None = None

    stop_event = threading.Event()
    original_sigint = signal.getsignal(signal.SIGINT)

    def _on_sigint(sig: int, frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _on_sigint)

    try:
        log("  Starting libtorrent session...")
        session = _create_session()
        handle = _add_magnet(session, magnet, save_path, trackers)

        torrent_info = _wait_for_metadata(session, handle, log=log)
        log(f"  Torrent: {torrent_info.name()}")

        file_idx, filename, file_size = _select_media_file(torrent_info, handle)
        log(f"  Media:   {filename} ({_format_speed(file_size).replace('/s', '')})")

        first_piece, last_piece = _file_piece_range(torrent_info, file_idx)
        _prioritize_for_streaming(handle, first_piece, last_piece)

        media_path = save_path / torrent_info.files().file_path(file_idx)
        handler = _make_handler(media_path, file_size, torrent_info, handle, file_idx)
        server = _start_http_server(port, handler)

        ext = Path(filename).suffix or ".mp4"
        stream_url = f"http://127.0.0.1:{port}/stream{ext}"
        log(f"  Server:  {stream_url}")

        piece_length = torrent_info.piece_length()
        buffer_pieces = max(20, (STREAM_BUFFER_MB * 1024 * 1024) // piece_length)
        target_piece = min(first_piece + buffer_pieces, last_piece)

        log(f"  Buffering ({STREAM_BUFFER_MB}MB)...")
        while not stop_event.is_set():
            status = handle.status()
            pieces = status.pieces
            buffered = all(pieces[p] for p in range(first_piece, target_piece + 1))
            if buffered:
                break
            have = sum(1 for p in range(first_piece, target_piece + 1) if pieces[p])
            total = target_piece - first_piece + 1
            pct = have / total * 100
            log(f"\r  Buffering: {pct:.0f}% ({have}/{total} pieces)", end="", flush=True)
            time.sleep(0.5)

        log("\r" + " " * 60 + "\r", end="", flush=True)

        if stop_event.is_set():
            return {"status": "cancelled"}

        resolved_player: str | None = None
        if player == "auto":
            resolved_player = _detect_player()
        else:
            resolved_player = player if shutil.which(player) else None

        if resolved_player:
            log(f"  Launching {resolved_player}...")
            player_proc = _launch_player(resolved_player, stream_url)
            if not player_proc and resolved_player != "iina":
                log(f"  Failed to launch {resolved_player}")
                log(f"  Open manually: {stream_url}")
        else:
            log(f"  No player found. Open manually: {stream_url}")

        if json_mode:
            import json
            json.dump({
                "status": "streaming",
                "url": stream_url,
                "player": resolved_player,
                "file": filename,
                "size": file_size,
            }, sys.stdout)
            print()

        while not stop_event.is_set():
            session.post_torrent_updates()
            status = handle.status()

            if not json_mode:
                _print_status(status, log)

            if player_proc and player_proc.poll() is not None:
                log("\n  Player exited.")
                if not json_mode and sys.stdin.isatty():
                    try:
                        answer = input("  Continue downloading? (y/n): ").strip().lower()
                        if answer not in ("y", "yes"):
                            break
                        player_proc = None
                    except EOFError:
                        break
                else:
                    break

            if status.is_seeding:
                log("\n  Download complete.")
                break

            time.sleep(1)

        return {
            "status": "completed" if handle and handle.status().is_seeding else "stopped",
            "url": stream_url,
            "player": resolved_player,
            "file": filename,
            "path": str(save_path),
            "keep": keep,
        }

    except StreamError:
        raise
    except Exception as e:
        raise StreamError(str(e)) from e
    finally:
        signal.signal(signal.SIGINT, original_sigint)
        _cleanup(server, session, handle, save_path, keep, log)
