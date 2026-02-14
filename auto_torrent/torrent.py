"""Shared libtorrent primitives for streaming and downloading."""

import time
from pathlib import Path
from typing import Callable

import libtorrent as lt

from .config import DHT_BOOTSTRAP_NODES


class TorrentError(Exception):
    pass


def create_session(listen_port: int = 6881) -> lt.session:
    settings = {
        "listen_interfaces": f"0.0.0.0:{listen_port}",
        "enable_dht": True,
        "enable_lsd": True,
        "enable_upnp": True,
        "enable_natpmp": True,
    }
    ses = lt.session(settings)
    for host, port in DHT_BOOTSTRAP_NODES:
        ses.add_dht_router(host, port)
    return ses


def add_magnet(
    session: lt.session,
    magnet: str,
    save_path: Path,
    trackers: list[str],
    sequential: bool = False,
) -> lt.torrent_handle:
    params = lt.parse_magnet_uri(magnet)
    params.save_path = str(save_path)

    if sequential:
        params.flags |= lt.torrent_flags.sequential_download

    for tracker in trackers:
        params.trackers.append(tracker)

    return session.add_torrent(params)


def wait_for_metadata(
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
            raise TorrentError(
                f"No metadata received after {timeout}s — "
                "check your connection or try a torrent with more seeders"
            )
        log(f"\r  Waiting for metadata... {remaining}s remaining", end="", flush=True)
        time.sleep(1)
    log("\r" + " " * 60 + "\r", end="", flush=True)
    return handle.torrent_file()


def format_speed(bps: int) -> str:
    if bps >= 1024**2:
        return f"{bps / 1024**2:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps} B/s"
