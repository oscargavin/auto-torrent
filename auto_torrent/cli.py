"""Search AudiobookBay / The Pirate Bay and download via aria2."""

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import abb, tpb
from .abb import ABBError
from .config import DEFAULT_LIMIT, DOWNLOAD_DIR, MIN_SCORE, SCRAPE_WORKERS, STATE_DIR, STREAM_DIR, STREAM_PORT, get_proxy
from .openlibrary import download_cover, lookup_book
from .scoring import quick_score, score_and_sort
from .tpb import TPBError, TPBResult
from .types import BookMetadata, ScoredResult, SearchResult

SUSPICIOUS_EXTENSIONS = {
    ".exe", ".bat", ".scr", ".msi", ".cmd", ".ps1",
    ".vbs", ".js", ".wsf", ".com", ".pif", ".reg",
}

_quiet = False


def _log(msg: str, **kwargs) -> None:
    if not _quiet:
        print(msg, **kwargs)


def _fan_out_search(book: BookMetadata) -> list[SearchResult]:
    queries = [book.title]
    if book.series:
        queries.append(book.series)
    if book.author:
        queries.append(book.author)

    seen: set[str] = set()
    all_results: list[SearchResult] = []

    for q in queries:
        _log(f'  Searching ABB: "{q}"')
        for result in abb.search(q, max_pages=1):
            if result.link not in seen:
                seen.add(result.link)
                all_results.append(result)

    return all_results


def _enrich_results(results: list[SearchResult]) -> list[SearchResult]:
    enriched: list[SearchResult | None] = [None] * len(results)

    def fetch(idx: int, result: SearchResult) -> tuple[int, SearchResult]:
        try:
            return idx, abb.get_details(result)
        except Exception:
            return idx, result

    total = len(results)
    done = 0
    with ThreadPoolExecutor(max_workers=SCRAPE_WORKERS) as pool:
        futures = {pool.submit(fetch, i, r): i for i, r in enumerate(results)}
        for future in as_completed(futures):
            idx, merged = future.result()
            enriched[idx] = merged
            done += 1
            _log(f"\r  Loading details... {done}/{total}", end="", flush=True)

    _log("\r" + " " * 40 + "\r", end="", flush=True)
    return [r for r in enriched if r is not None]


def _format_result(i: int, scored: ScoredResult) -> str:
    r = scored.result
    parts: list[str] = []

    if r.narrator:
        parts.append(f"Read by {r.narrator}")

    if r.format and r.bitrate:
        parts.append(f"{r.format} {r.bitrate}")
    elif r.format:
        parts.append(r.format)

    if r.file_size:
        parts.append(r.file_size)

    if r.abridged is False:
        parts.append("Unabridged")
    elif r.abridged is True:
        parts.append("Abridged")

    if r.language and r.language.lower() != "english":
        parts.append(r.language)

    if r.posted:
        parts.append(r.posted)

    parts.append(f"{scored.score}% match")

    info_line = " | ".join(parts) if parts else "No details"
    return f"  [{i:>2}] {r.title}\n       {info_line}"


def _format_tpb_result(i: int, r: TPBResult) -> str:
    parts = [p for p in [r.file_size, f"{r.seeders} seeds", r.category, f"{r.score}%"] if p]
    info_line = " | ".join(parts) if parts else "No details"
    line = f"  [{i:>2}] {r.title}\n       {info_line}"
    if r.warning:
        line += f"\n       ⚠ {r.warning}"
    return line


def _sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


def _prompt_choice(prompt: str, max_val: int) -> int | None:
    while True:
        raw = input(prompt).strip()
        if raw.lower() in ("q", "quit", "exit"):
            return None
        try:
            val = int(raw)
            if 1 <= val <= max_val:
                return val
            print(f"  Pick 1-{max_val}, or q to quit")
        except ValueError:
            print(f"  Pick 1-{max_val}, or q to quit")


def _scored_to_dict(s: ScoredResult) -> dict:
    d = asdict(s.result)
    d["score"] = s.score
    return d


def _json_out(data: dict) -> None:
    json.dump(data, sys.stdout, indent=2)
    print()


def _json_error(error: str) -> None:
    json.dump({"error": error}, sys.stdout)
    print()


# -- State file helpers for background downloads --

def _write_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{state['id']}.json"
    path.write_text(json.dumps(state, indent=2))


def _read_state(download_id: str) -> dict | None:
    path = STATE_DIR / f"{download_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _read_all_states() -> list[dict]:
    if not STATE_DIR.exists():
        return []
    states = []
    for f in sorted(STATE_DIR.glob("*.json")):
        try:
            states.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return states


def _check_pid(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _resolve_status(state: dict) -> str:
    if state.get("status") != "downloading":
        return state.get("status", "unknown")
    pid = state.get("pid")
    if pid and not _check_pid(pid):
        path = Path(state.get("path", ""))
        if path.exists() and any(path.iterdir()):
            return "completed"
        return "failed"
    return "downloading"


# -- Safety checks --

def _scan_for_suspicious_files(directory: str) -> list[str]:
    suspect: list[str] = []
    for path in Path(directory).rglob("*"):
        if path.is_file() and path.suffix.lower() in SUSPICIOUS_EXTENSIONS:
            suspect.append(str(path.relative_to(directory)))
    return suspect


# -- Download execution --

def _aria2c_args(dest: Path, magnet: str) -> list[str]:
    return [
        "aria2c",
        "--dir", str(dest),
        "--seed-time=0",
        "--summary-interval=5",
        "--bt-stop-timeout=300",
        "--enable-dht=true",
        "--enable-peer-exchange=true",
        "--bt-enable-lpd=true",
        magnet,
    ]


def _execute_download_fg(
    title: str,
    magnet: str,
    cover_id: int | None,
) -> dict:
    dest = DOWNLOAD_DIR / _sanitize(title)
    dest.mkdir(parents=True, exist_ok=True)

    download_info: dict = {"path": str(dest), "cover": None, "exit_code": None}

    if cover_id:
        cover = download_cover(cover_id, dest)
        if cover:
            download_info["cover"] = str(cover)
            _log(f"  Cover saved: {cover}")
        else:
            _log("  Cover not available")

    _log(f"\n  Downloading to: {dest}")
    _log("  Starting aria2c... (Ctrl+C to cancel)\n")
    proc = subprocess.run(_aria2c_args(dest, magnet))
    download_info["exit_code"] = proc.returncode

    if proc.returncode == 0:
        suspect = _scan_for_suspicious_files(str(dest))
        if suspect:
            download_info["warnings"] = [f"Suspicious file: {f}" for f in suspect]
            _log("\n  ⚠ WARNING: Suspicious files detected in download:")
            for f in suspect:
                _log(f"    - {f}")
            _log("  These may contain malware. Inspect before opening.\n")

    return download_info


def _execute_download_bg(
    title: str,
    magnet: str,
    cover_id: int | None,
) -> dict:
    dest = DOWNLOAD_DIR / _sanitize(title)
    dest.mkdir(parents=True, exist_ok=True)

    download_id = uuid.uuid4().hex[:8]
    cover_path: str | None = None

    if cover_id:
        cover = download_cover(cover_id, dest)
        if cover:
            cover_path = str(cover)

    proc = subprocess.Popen(
        _aria2c_args(dest, magnet),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    state = {
        "id": download_id,
        "pid": proc.pid,
        "magnet": magnet,
        "title": title,
        "path": str(dest),
        "cover": cover_path,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "downloading",
    }
    _write_state(state)
    return state


# -- Subcommand handlers --

def _resolve_proxy(args: argparse.Namespace) -> str | None:
    return getattr(args, "proxy", None) or get_proxy()


def cmd_search(args: argparse.Namespace) -> None:
    global _quiet

    json_mode = args.json
    _quiet = json_mode
    limit = args.limit
    source = args.source

    proxy = _resolve_proxy(args)
    if proxy:
        abb.configure(proxy=proxy)

    query = " ".join(args.query) if args.query else ""
    narrator_pref = " ".join(args.narrator) if args.narrator else None

    if not query:
        if json_mode:
            _json_error("No query provided")
            return
        query = input("Search: ").strip()
    if not query:
        return

    stream_mode = args.stream
    player = args.player
    port = args.port
    keep = args.keep

    if source == "tpb":
        _cmd_search_tpb(query, limit, json_mode, args.auto, args.category, args.min_seeds, args.quality, stream_mode, player, port, keep, proxy)
    else:
        _cmd_search_abb(query, limit, json_mode, args.auto, narrator_pref, stream_mode, player, port, keep)


def _cmd_search_tpb(
    query: str,
    limit: int,
    json_mode: bool,
    auto: bool,
    category: str,
    min_seeds: int,
    quality: str,
    stream_mode: bool = False,
    player: str = "auto",
    port: int = STREAM_PORT,
    keep: bool = False,
    proxy: str | None = None,
) -> None:
    _log(f"\n  Searching The Pirate Bay: {query}")
    if category != "all":
        _log(f"  Category: {category} | Min seeds: {min_seeds} | Quality: {quality}")

    try:
        results = tpb.search(query, category=category, min_seeds=min_seeds, quality=quality, proxy=proxy)
    except TPBError as e:
        if json_mode:
            _json_error(str(e))
        else:
            print(f"\n  {e}")
        return

    if not results:
        if json_mode:
            _json_error("No results on The Pirate Bay")
        else:
            print("  No results on The Pirate Bay.")
        return

    results = results[:limit]

    def _action(title: str, magnet: str) -> dict | None:
        if stream_mode:
            return _execute_stream(magnet, json_mode, player, port, keep)
        return _execute_download_fg(title, magnet, None)

    action_word = "Stream" if stream_mode else "Download"

    if json_mode:
        output: dict = {
            "results": [asdict(r) for r in results],
            "download": None,
        }
        if auto:
            best = results[0]
            output["download"] = _action(best.title, best.magnet)
        _json_out(output)
        return

    if auto:
        best = results[0]
        print(f"\n  Auto-selected: {best.title}")
        print(f"  {best.file_size} | {best.seeders} seeds | {best.category}")
        if best.warning:
            print(f"  ⚠ {best.warning}")
        print()
        _action(best.title, best.magnet)
        return

    print()
    for i, r in enumerate(results, 1):
        print(_format_tpb_result(i, r))

    print()
    pick = _prompt_choice(f"Select (1-{len(results)}), or q: ", len(results))
    if pick is None:
        return

    selected = results[pick - 1]
    print()
    confirm = input(f"{action_word}? (y/n): ").strip().lower()
    if confirm not in ("y", "yes"):
        print("Cancelled.")
        return

    _action(selected.title, selected.magnet)


def _cmd_search_abb(
    query: str,
    limit: int,
    json_mode: bool,
    auto: bool,
    narrator_pref: str | None,
    stream_mode: bool = False,
    player: str = "auto",
    port: int = STREAM_PORT,
    keep: bool = False,
) -> None:
    _log(f"\n  Looking up: {query}")
    book = lookup_book(query)
    if not book:
        if json_mode:
            _json_error("No results on Open Library")
        else:
            print("  No results on Open Library.")
        return

    _log(f"\n  Title:  {book.title}")
    _log(f"  Author: {book.author}")
    if book.series:
        _log(f"  Series: {book.series}")
    if book.year:
        _log(f"  Year:   {book.year}")
    if narrator_pref:
        _log(f"  Prefer: {narrator_pref}")

    _log("")
    try:
        results = _fan_out_search(book)
    except ABBError as e:
        if json_mode:
            _json_error(str(e))
        else:
            print(f"\n  {e}")
        return

    if not results:
        if json_mode:
            _json_error("No results on AudiobookBay")
        else:
            print("  No results on AudiobookBay.")
        return

    max_enrich = limit * 2
    if len(results) > max_enrich:
        results.sort(key=lambda r: quick_score(r, book), reverse=True)
        results = results[:max_enrich]

    _log(f"\n  Found {len(results)} candidates, loading details...")
    results = _enrich_results(results)

    scored = score_and_sort(results, book, narrator_pref, MIN_SCORE)
    if not scored:
        if json_mode:
            _json_error("No good matches found")
        else:
            print("  No good matches found.")
        return

    scored = scored[:limit]

    def _action(title: str, magnet: str, cover_id: int | None) -> dict | None:
        if stream_mode:
            return _execute_stream(magnet, json_mode, player, port, keep)
        return _execute_download_fg(title, magnet, cover_id)

    action_word = "Stream" if stream_mode else "Download"

    if json_mode:
        output: dict = {
            "book": asdict(book),
            "results": [_scored_to_dict(s) for s in scored],
            "download": None,
        }
        if auto:
            best = scored[0]
            output["download"] = _action(book.title, best.result.magnet, book.cover_id)
        _json_out(output)
        return

    if auto:
        best = scored[0]
        print(f"\n  Auto-selected: {best.result.title} ({best.score}% match)")
        if best.result.narrator:
            print(f"  Narrator: {best.result.narrator}")
        fmt = best.result.format or "?"
        size = best.result.file_size or "?"
        print(f"  {fmt} | {size}")
        print()
        _action(book.title, best.result.magnet, book.cover_id)
        return

    print()
    for i, s in enumerate(scored, 1):
        print(_format_result(i, s))

    print()
    pick = _prompt_choice(f"Select (1-{len(scored)}), or q: ", len(scored))
    if pick is None:
        return

    selected = scored[pick - 1]
    if selected.result.description:
        desc = selected.result.description
        print(f"\n  {desc[:300]}{'...' if len(desc) > 300 else ''}")

    print()
    confirm = input(f"{action_word}? (y/n): ").strip().lower()
    if confirm not in ("y", "yes"):
        print("Cancelled.")
        return

    _action(book.title, selected.result.magnet, book.cover_id)


def cmd_download(args: argparse.Namespace) -> None:
    global _quiet

    json_mode = args.json
    _quiet = json_mode
    magnet = args.magnet
    title = args.title
    cover_id = args.cover_id
    bg = args.bg

    if bg:
        state = _execute_download_bg(title, magnet, cover_id)
        if json_mode:
            _json_out(state)
        else:
            print(f"  Download started in background (id: {state['id']})")
            print(f"  Path: {state['path']}")
            print(f"  Check progress: auto-torrent status {state['id']}")
    else:
        info = _execute_download_fg(title, magnet, cover_id)
        if json_mode:
            _json_out(info)
        else:
            if info["exit_code"] == 0:
                print(f"\n  Download complete: {info['path']}")
            else:
                print(f"\n  aria2c exited with code {info['exit_code']}")


def cmd_status(args: argparse.Namespace) -> None:
    json_mode = args.json
    download_id = args.download_id

    if download_id:
        state = _read_state(download_id)
        if not state:
            if json_mode:
                _json_error(f"No download found with id: {download_id}")
            else:
                print(f"  No download found with id: {download_id}")
            return

        state["status"] = _resolve_status(state)
        _write_state(state)

        if json_mode:
            _json_out(state)
        else:
            print(f"  ID:      {state['id']}")
            print(f"  Title:   {state['title']}")
            print(f"  Status:  {state['status']}")
            print(f"  Path:    {state['path']}")
            if state.get("cover"):
                print(f"  Cover:   {state['cover']}")
            print(f"  Started: {state['started_at']}")
        return

    # List all downloads
    states = _read_all_states()
    if not states:
        if json_mode:
            _json_out({"downloads": []})
        else:
            print("  No downloads found.")
        return

    for s in states:
        s["status"] = _resolve_status(s)
        _write_state(s)

    if json_mode:
        _json_out({"downloads": states})
    else:
        for s in states:
            status_icon = "↓" if s["status"] == "downloading" else "✓"
            print(f"  {status_icon} [{s['id']}] {s['title']} — {s['status']}")


def _execute_stream(
    magnet: str,
    json_mode: bool,
    player: str = "auto",
    port: int = STREAM_PORT,
    keep: bool = False,
) -> dict | None:
    from .stream import StreamError, stream

    try:
        return stream(
            magnet=magnet, player=player, port=port,
            keep=keep, json_mode=json_mode, log=_log,
        )
    except StreamError as e:
        if json_mode:
            _json_error(str(e))
        else:
            print(f"\n  Error: {e}")
        return None


def cmd_stream(args: argparse.Namespace) -> None:
    global _quiet

    json_mode = args.json
    _quiet = json_mode

    result = _execute_stream(
        magnet=args.magnet,
        json_mode=json_mode,
        player=args.player,
        port=args.port,
        keep=args.keep,
    )
    if result is None:
        sys.exit(1)
    if json_mode:
        _json_out(result)


# -- Parser --

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto-torrent",
        description="Search AudiobookBay / The Pirate Bay and download via aria2.",
    )

    subs = parser.add_subparsers(dest="command")

    # search
    search_p = subs.add_parser("search", help="Search for a torrent")
    search_p.add_argument("query", nargs="*", help="Title to search for")
    search_p.add_argument("--json", action="store_true", help="Output structured JSON")
    search_p.add_argument("--auto", action="store_true", help="Auto-select best match and download")
    search_p.add_argument("--narrator", nargs="+", help="Preferred narrator name (ABB only)")
    search_p.add_argument(
        "--source", choices=["abb", "tpb"], default="abb",
        help="Search source: abb (AudiobookBay) or tpb (The Pirate Bay)",
    )
    search_p.add_argument(
        "--category", default="video",
        help="TPB category filter: video (default), audio, apps, games, all",
    )
    search_p.add_argument(
        "--min-seeds", type=int, default=5,
        help="Minimum seeders to include (default: 5, TPB only)",
    )
    search_p.add_argument(
        "--quality", choices=["2160p", "1080p", "720p", "480p"], default="1080p",
        help="Preferred resolution for scoring (default: 1080p, TPB only)",
    )
    search_p.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=f"Max results to return (default: {DEFAULT_LIMIT})",
    )
    search_p.add_argument("--stream", action="store_true", help="Stream instead of download")
    search_p.add_argument(
        "--player", default="auto",
        help="Media player for streaming: mpv, vlc, iina, or auto (default: auto)",
    )
    search_p.add_argument(
        "--port", type=int, default=STREAM_PORT,
        help=f"HTTP server port for streaming (default: {STREAM_PORT})",
    )
    search_p.add_argument("--keep", action="store_true", help="Keep files after streaming")
    search_p.add_argument("--proxy", help="Proxy URL (socks5h://user:pass@host:port or http://host:port)")

    # download
    dl_p = subs.add_parser("download", help="Download a specific audiobook by magnet")
    dl_p.add_argument("magnet", help="Magnet URI to download")
    dl_p.add_argument("--title", required=True, help="Book title (for directory naming)")
    dl_p.add_argument("--cover-id", type=int, default=None, help="Open Library cover ID")
    dl_p.add_argument("--json", action="store_true", help="Output structured JSON")
    dl_p.add_argument("--bg", action="store_true", help="Download in background")

    # status
    status_p = subs.add_parser("status", help="Check download status")
    status_p.add_argument("download_id", nargs="?", default=None, help="Specific download ID")
    status_p.add_argument("--json", action="store_true", help="Output structured JSON")

    # stream
    stream_p = subs.add_parser("stream", help="Stream a torrent to a media player")
    stream_p.add_argument("magnet", help="Magnet URI to stream")
    stream_p.add_argument(
        "--player", default="auto",
        help="Media player: mpv, vlc, iina, or auto (default: auto)",
    )
    stream_p.add_argument(
        "--port", type=int, default=STREAM_PORT,
        help=f"HTTP server port (default: {STREAM_PORT})",
    )
    stream_p.add_argument(
        "--save-path", default=None,
        help=f"Save directory (default: {STREAM_DIR})",
    )
    stream_p.add_argument("--keep", action="store_true", help="Keep files after streaming")
    stream_p.add_argument("--json", action="store_true", help="Output structured JSON")
    stream_p.add_argument("--proxy", help="Proxy URL (socks5h://user:pass@host:port or http://host:port)")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "search":
        cmd_search(args)
    elif args.command == "download":
        cmd_download(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "stream":
        cmd_stream(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
