"""Libtorrent-based torrent download engine."""

import json
import signal
import time
import threading
from pathlib import Path
from typing import Callable

import libtorrent as lt

from .config import DEFAULT_TRACKERS, DOWNLOAD_PROGRESS_INTERVAL
from .torrent import TorrentError, create_session, add_magnet, wait_for_metadata, format_speed


def download_torrent(
    magnet: str,
    dest: Path,
    trackers: list[str] | None = None,
    state_file: Path | None = None,
    log: Callable[..., None] = print,
    stop_event: threading.Event | None = None,
) -> dict:
    """Download a torrent to dest. Returns status dict.

    If state_file is provided, writes progress updates periodically
    (used by background download processes).
    """
    if trackers is None:
        trackers = list(DEFAULT_TRACKERS)

    if stop_event is None:
        stop_event = threading.Event()

    dest.mkdir(parents=True, exist_ok=True)

    session = create_session(listen_port=6881)
    handle = add_magnet(session, magnet, dest, trackers, sequential=False)

    try:
        torrent_info = wait_for_metadata(session, handle, timeout=120, log=log)
        log(f"  Torrent: {torrent_info.name()} ({torrent_info.num_files()} files)")

        last_progress_write = 0.0

        while not stop_event.is_set():
            session.post_torrent_updates()
            status = handle.status()

            log(
                f"\r  {status.progress * 100:.1f}% | "
                f"{format_speed(status.download_rate)} down | "
                f"{status.num_peers} peers",
                end="", flush=True,
            )

            if state_file and (time.monotonic() - last_progress_write) >= DOWNLOAD_PROGRESS_INTERVAL:
                _update_state_progress(state_file, status.progress)
                last_progress_write = time.monotonic()

            if status.is_seeding:
                log(f"\n  Download complete: {dest}")
                if state_file:
                    _update_state_progress(state_file, 1.0, status="completed")
                break

            time.sleep(1)

        if stop_event.is_set():
            log("\n  Download interrupted, saving resume data...")
            _save_resume_data(session, handle)
            return {"status": "interrupted", "path": str(dest), "progress": status.progress}

        return {"status": "completed", "path": str(dest), "progress": 1.0}

    except TorrentError:
        raise
    except Exception as e:
        raise TorrentError(str(e)) from e
    finally:
        try:
            _save_resume_data(session, handle)
        except Exception:
            pass
        session.remove_torrent(handle)


def _save_resume_data(session: lt.session, handle: lt.torrent_handle) -> None:
    """Request and wait for resume data save (enables fast restart)."""
    handle.save_resume_data(lt.save_resume_flags_t.save_info_dict)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        alerts = session.pop_alerts()
        for alert in alerts:
            if isinstance(alert, lt.save_resume_data_alert):
                return
            if isinstance(alert, lt.save_resume_data_failed_alert):
                return
        time.sleep(0.1)


def _update_state_progress(state_file: Path, progress: float, status: str | None = None) -> None:
    """Update progress (and optionally status) in a state JSON file."""
    try:
        state = json.loads(state_file.read_text())
        state["progress"] = round(progress, 4)
        if status:
            state["status"] = status
        state_file.write_text(json.dumps(state, indent=2))
    except (json.JSONDecodeError, OSError):
        pass


def run_background_download(
    magnet: str,
    dest: str,
    state_file: str,
    trackers: list[str] | None = None,
) -> None:
    """Entry point for multiprocessing.Process background downloads."""
    stop = threading.Event()

    def _on_signal(sig: int, frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    def _quiet_log(msg: str, **kwargs: object) -> None:
        pass

    try:
        download_torrent(
            magnet=magnet,
            dest=Path(dest),
            trackers=trackers,
            state_file=Path(state_file),
            log=_quiet_log,
            stop_event=stop,
        )
    except Exception:
        try:
            _update_state_progress(Path(state_file), 0.0, status="failed")
        except Exception:
            pass
