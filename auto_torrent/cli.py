"""Search AudiobookBay / The Pirate Bay and download via libtorrent."""

import argparse
import json
import multiprocessing
import os
import re
import subprocess
import sys
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import libtorrent as lt

from . import abb, tpb
from .abb import ABBError
from .config import (
    DEFAULT_LIMIT, DEFAULT_TRACKERS, DHT_BOOTSTRAP_NODES, DOWNLOAD_DIR,
    LLM_MODEL, LLM_TIMEOUT,
    MIN_SCORE, PROBE_CANDIDATES, PROBE_TIMEOUT, SCRAPE_WORKERS, STATE_DIR,
    STREAM_DIR, STREAM_PORT, get_proxy,
)
from .download import download_torrent, run_background_download
from .openlibrary import download_cover, lookup_book
from .scoring import quick_score, score_and_sort
from .torrent import TorrentError
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


def _llm_parse_query(raw_query: str) -> tuple[BookMetadata, str | None]:
    """Use LLM to extract structured book metadata from a freeform query."""
    prompt = f"""You are a librarian. Extract structured audiobook metadata from this search query.

<query>{raw_query}</query>

<instructions>
Parse the query into structured fields. Use your knowledge of books, authors, and series to fill in missing information.
- title: The book title (clean, canonical form — e.g. "The Wise Man's Fear" not "wise mans fear")
- author: The author's full name (infer from title if not in query)
- series: The series name if applicable (e.g. "The Kingkiller Chronicle", "Red Rising Saga")
- narrator: Preferred narrator if mentioned in the query (e.g. "rupert degas" from "wise man's fear rupert degas")
If you cannot confidently identify a field, leave it as null.
</instructions>

<examples>
<example>
Query: "wise man's fear rupert degas"
title: "The Wise Man's Fear", author: "Patrick Rothfuss", series: "The Kingkiller Chronicle", narrator: "Rupert Degas"
</example>
<example>
Query: "red rising book 1"
title: "Red Rising", author: "Pierce Brown", series: "Red Rising Saga", narrator: null
</example>
<example>
Query: "project hail mary"
title: "Project Hail Mary", author: "Andy Weir", series: null, narrator: null
</example>
</examples>"""

    schema = json.dumps({
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Canonical book title"},
            "author": {"type": ["string", "null"], "description": "Author full name or null"},
            "series": {"type": ["string", "null"], "description": "Series name or null"},
            "narrator": {"type": ["string", "null"], "description": "Preferred narrator or null"},
        },
        "required": ["title"],
    })

    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}

    result = subprocess.run(
        [
            "claude", "-p", prompt,
            "--model", LLM_MODEL,
            "--output-format", "json",
            "--json-schema", schema,
            "--max-turns", "2",
        ],
        capture_output=True, text=True, timeout=LLM_TIMEOUT,
        env=env,
    )

    parsed = json.loads(result.stdout)
    s = parsed["structured_output"]

    return BookMetadata(
        title=s["title"],
        author=s.get("author") or "",
        series=s.get("series"),
    ), s.get("narrator")


def _fan_out_search(book: BookMetadata, raw_query: str | None = None) -> list[SearchResult]:
    queries = [book.title]
    if raw_query and raw_query.lower() != book.title.lower():
        queries.append(raw_query)
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


# -- Seed probing --

def _probe_seeds_batch(magnets: list[str], timeout: int = PROBE_TIMEOUT) -> dict[str, int]:
    ses = lt.session({
        "listen_interfaces": "0.0.0.0:6891",
        "enable_dht": True,
        "enable_lsd": True,
        "enable_upnp": False,
        "enable_natpmp": False,
        "download_rate_limit": 1024,
        "upload_rate_limit": 1024,
    })
    for host, port in DHT_BOOTSTRAP_NODES:
        ses.add_dht_router(host, port)

    handles: list[tuple[str, lt.torrent_handle]] = []
    for magnet in magnets:
        params = lt.parse_magnet_uri(magnet)
        params.save_path = "/tmp/auto-torrent-probe"
        params.flags |= lt.torrent_flags.upload_mode
        for tracker in DEFAULT_TRACKERS:
            params.trackers.append(tracker)
        handles.append((magnet, ses.add_torrent(params)))

    results: dict[str, int] = {m: 0 for m in magnets}
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        ses.post_torrent_updates()
        for magnet, handle in handles:
            peers = handle.status().num_peers
            if peers > results[magnet]:
                results[magnet] = peers
        if any(v > 0 for v in results.values()):
            break
        time.sleep(0.5)

    for _, handle in handles:
        ses.remove_torrent(handle)
    return results


# -- Download execution --

def _execute_download_fg(
    title: str,
    magnet: str,
    cover_id: int | None,
) -> dict:
    dest = DOWNLOAD_DIR / _sanitize(title)
    dest.mkdir(parents=True, exist_ok=True)

    download_info: dict = {"path": str(dest), "cover": None, "status": None}

    if cover_id:
        cover = download_cover(cover_id, dest)
        if cover:
            download_info["cover"] = str(cover)
            _log(f"  Cover saved: {cover}")
        else:
            _log("  Cover not available")

    _log(f"\n  Downloading to: {dest}")
    _log("  Starting libtorrent... (Ctrl+C to cancel)\n")

    stop_event = threading.Event()
    original_sigint = signal.getsignal(signal.SIGINT)

    def _on_sigint(sig: int, frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _on_sigint)
    try:
        result = download_torrent(
            magnet=magnet, dest=dest, log=_log, stop_event=stop_event,
        )
        download_info["status"] = result["status"]
        download_info["progress"] = result.get("progress", 0)
    except TorrentError as e:
        download_info["status"] = "failed"
        download_info["error"] = str(e)
        _log(f"\n  Error: {e}")
    finally:
        signal.signal(signal.SIGINT, original_sigint)

    if download_info["status"] == "completed":
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

    state = {
        "id": download_id,
        "pid": None,
        "magnet": magnet,
        "title": title,
        "path": str(dest),
        "cover": cover_path,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "downloading",
        "progress": 0.0,
    }
    _write_state(state)

    state_file = str(STATE_DIR / f"{download_id}.json")
    proc = multiprocessing.Process(
        target=run_background_download,
        args=(magnet, str(dest), state_file, list(DEFAULT_TRACKERS)),
        daemon=False,
    )
    proc.start()

    state["pid"] = proc.pid
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
    auto = args.auto or stream_mode
    bg = args.bg
    player = args.player
    port = args.port
    keep = args.keep

    try:
        if source == "tpb":
            _cmd_search_tpb(query, limit, json_mode, auto, args.category, args.min_seeds, args.quality, stream_mode, player, port, keep, proxy, bg)
        else:
            _cmd_search_abb(query, limit, json_mode, auto, narrator_pref, stream_mode, player, port, keep, bg)
    except Exception as e:
        if json_mode:
            _json_error(f"{type(e).__name__}: {e}")
        else:
            print(f"\n  Error: {type(e).__name__}: {e}", file=sys.stderr)
            sys.exit(1)


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
    bg: bool = False,
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
        if bg:
            return _execute_download_bg(title, magnet, None)
        return _execute_download_fg(title, magnet, None)

    action_word = "Stream" if stream_mode else "Download"

    if json_mode:
        output: dict = {
            "results": [asdict(r) for r in results],
            "download": None,
        }
        if auto:
            _log(f"\n  Selecting from {len(results)} results...")
            chosen, peers, peer_counts, reason = _tpb_probe_and_select(query, results)
            output["download"] = _action(chosen.title, chosen.magnet)
            output["selected_index"] = results.index(chosen)
            output["peers"] = peers
            output["reason"] = reason
            if not any(v > 0 for v in peer_counts.values()):
                output["warning"] = "No results had active peers, using top match"
        _json_out(output)
        return

    if auto:
        _log(f"\n  Selecting from {len(results)} results...")
        chosen, peers, _, reason = _tpb_probe_and_select(query, results)
        if peers > 0:
            print(f"\n  Selected: {chosen.title} ({chosen.score}%, {peers} peer(s))")
        else:
            print(f"\n  No peers found, trying top match: {chosen.title}")
        print(f"  {chosen.file_size} | {chosen.seeders} seeds | {chosen.category}")
        if chosen.warning:
            print(f"  ⚠ {chosen.warning}")
        print()
        _action(chosen.title, chosen.magnet)
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


def _llm_pick_tpb(
    query: str,
    candidates: list[TPBResult],
    n: int = PROBE_CANDIDATES,
) -> tuple[list[int], str]:
    """Ask Sonnet to rank the top N TPB results. Returns (indices, reason)."""
    results_xml = []
    for i, r in enumerate(candidates):
        fields = [f"    <title>{r.title}</title>"]
        fields.append(f"    <size>{r.file_size}</size>")
        fields.append(f"    <seeders>{r.seeders}</seeders>")
        fields.append(f"    <category>{r.category}</category>")
        fields.append(f"    <uploader>{r.status}</uploader>")
        fields.append(f"    <score>{r.score}%</score>")
        if r.warning:
            fields.append(f"    <warning>{r.warning}</warning>")
        results_xml.append(f'  <result index="{i}">\n' + "\n".join(fields) + "\n  </result>")

    prompt = f"""You are a media search assistant. A user searched for a movie or TV show to stream/download and got multiple torrent results. Pick the {n} best results ranked from best to worst.

<query>{query}</query>

<results>
{chr(10).join(results_xml)}
</results>

<instructions>
Think step-by-step about which results best match the query, then return your top {n} picks.

Ranking criteria (in priority order):
1. Correct content — right movie/show, correct season/episode if specified, correct year if specified
2. Uploader trust — "vip" and "trusted" uploaders are safer than "member"
3. Quality — prefer 1080p/2160p BluRay/WEB-DL over lower quality or cam rips. Title usually contains resolution and source
4. Seeders — more seeders = faster download, but quality matters more than speed
5. Avoid red flags — warnings, suspiciously small file sizes for movies (<700MB for 1080p), wrong language
</instructions>

<examples>
<example>
Query: "inception 2010"
Result [0]: Inception.2010.CAM.XviD - 700 MB - 50 seeds - member
Result [1]: Inception.2010.1080p.BluRay.x264 - 2.1 GB - 120 seeds - vip
Result [2]: Inception.2010.2160p.UHD.BluRay - 15 GB - 30 seeds - trusted
Correct ranking: [1, 2, 0] — Result 1 is 1080p BluRay from VIP uploader with most seeds. Result 2 is 4K but fewer seeds. Result 0 is a cam rip.
</example>
<example>
Query: "breaking bad s01e01"
Result [0]: Breaking.Bad.S01-S05.Complete.1080p - 45 GB - 200 seeds - vip
Result [1]: Breaking.Bad.S01E01.1080p.BluRay - 1.5 GB - 80 seeds - trusted
Result [2]: Breaking.Bad.S01E01.720p.WEB-DL - 900 MB - 150 seeds - member
Correct ranking: [1, 2, 0] — User wants episode 1 specifically, not the full series pack. Result 1 is the exact episode in 1080p BluRay.
</example>
</examples>

Return exactly {n} indices as your top picks, ranked best-first."""

    schema = json.dumps({
        "type": "object",
        "properties": {
            "indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": f"Top {n} result indices, ranked best-first",
            },
            "reason": {"type": "string", "description": "One sentence explaining the top pick"},
        },
        "required": ["indices", "reason"],
    })

    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}

    result = subprocess.run(
        [
            "claude", "-p", prompt,
            "--model", LLM_MODEL,
            "--output-format", "json",
            "--json-schema", schema,
            "--max-turns", "2",
        ],
        capture_output=True, text=True, timeout=LLM_TIMEOUT,
        env=env,
    )

    parsed = json.loads(result.stdout)
    structured = parsed["structured_output"]
    indices = structured["indices"]
    reason = structured.get("reason", "")

    valid = [i for i in indices if 0 <= i < len(candidates)]
    if not valid:
        return list(range(min(n, len(candidates)))), "LLM returned no valid indices, using score order"

    return valid[:n], reason


def _tpb_probe_and_select(
    query: str,
    results: list[TPBResult],
) -> tuple[TPBResult, int, dict[str, int], str]:
    """LLM picks top TPB results, probe for peers, pick best. Returns (chosen, peers, counts, reason)."""
    reason = ""
    llm_picks: list[TPBResult] = []

    try:
        indices, reason = _llm_pick_tpb(query, results)
        llm_picks = [results[i] for i in indices]
        _log(f"  LLM ranked: {indices} — {reason}")
    except Exception as e:
        _log(f"  LLM selection failed ({e}), using score order")
        llm_picks = results[:PROBE_CANDIDATES]
        reason = "heuristic fallback"

    magnets = [r.magnet for r in llm_picks]
    peer_counts = _probe_seeds_batch(magnets)

    chosen = next(
        (r for r in llm_picks if peer_counts.get(r.magnet, 0) > 0),
        llm_picks[0],
    )

    return chosen, peer_counts.get(chosen.magnet, 0), peer_counts, reason


def _build_llm_prompt(
    query: str,
    candidates: list[ScoredResult],
    narrator_pref: str | None,
    n: int,
) -> str:
    """Build structured prompt for audiobook result selection."""
    results_xml = []
    for i, s in enumerate(candidates):
        r = s.result
        fields = [f"    <title>{r.title}</title>"]
        if r.narrator:
            fields.append(f"    <narrator>{r.narrator}</narrator>")
        if r.format:
            fields.append(f"    <format>{r.format}{(' ' + r.bitrate) if r.bitrate else ''}</format>")
        if r.file_size:
            fields.append(f"    <size>{r.file_size}</size>")
        fields.append(f"    <score>{s.score}%</score>")
        if r.abridged is not None:
            fields.append(f"    <abridged>{'yes' if r.abridged else 'no'}</abridged>")
        if r.language and r.language.lower() != "english":
            fields.append(f"    <language>{r.language}</language>")
        if r.description:
            fields.append(f"    <description>{r.description[:300]}</description>")
        results_xml.append(f'  <result index="{i}">\n' + "\n".join(fields) + "\n  </result>")

    narrator_line = ""
    if narrator_pref:
        narrator_line = f"\nThe user specifically requested narrator: {narrator_pref}. Prioritize exact narrator matches above all other criteria.\n"

    return f"""You are an audiobook search assistant. A user searched for an audiobook and got multiple results. Your job is to pick the {n} results that best match their search intent, ranked from best to worst.

<query>{query}</query>
{narrator_line}
<results>
{chr(10).join(results_xml)}
</results>

<instructions>
Think step-by-step about which results best match the query, then return your top {n} picks.

Ranking criteria (in priority order):
1. Narrator match — if the user specified a narrator, that is the top priority
2. Correct book — "red rising 1" means the FIRST book, not book 4 or 6. Match series number if specified
3. Standard audiobook over dramatized/full-cast adaptations, unless the user asked for those
4. Unabridged over abridged
5. Higher match score
6. M4B format over MP3 (better chapter support)
7. Higher bitrate
</instructions>

<examples>
<example>
Query: "wise man's fear narrator rupert degas"
Result [0]: The Wise Man's Fear - Narrator: Nick Podehl - MP3 - 95%
Result [1]: The Wise Man's Fear - Narrator: Rupert Degas - M4B - 90%
Result [2]: The Name of the Wind - Narrator: Rupert Degas - M4B - 60%
Correct ranking: [1, 0, 2] — Result 1 matches the requested narrator Rupert Degas AND the correct book. Result 0 has the right book but wrong narrator. Result 2 has right narrator but wrong book.
</example>
<example>
Query: "red rising book 1"
Result [0]: Red Rising (Dramatized Adaptation) - 100%
Result [1]: Red Rising Saga 4 - Iron Gold - 100%
Result [2]: Red Rising - Narrator: Tim Gerard Reynolds - M4B - 100%
Result [3]: Light Bringer (Red Rising #06) - 100%
Correct ranking: [2, 0, 1] — Result 2 is the standard audiobook of book 1. Result 0 is book 1 but dramatized. Result 1 is book 4, not book 1. Result 3 is book 6.
</example>
</examples>

Return exactly {n} indices as your top picks, ranked best-first."""


def _llm_pick_top(
    query: str,
    candidates: list[ScoredResult],
    narrator_pref: str | None = None,
    n: int = PROBE_CANDIDATES,
) -> tuple[list[int], str]:
    """Ask Sonnet to rank the top N results from all candidates. Returns (indices, reason)."""
    prompt = _build_llm_prompt(query, candidates, narrator_pref, n)

    schema = json.dumps({
        "type": "object",
        "properties": {
            "indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": f"Top {n} result indices, ranked best-first",
            },
            "reason": {"type": "string", "description": "One sentence explaining the top pick"},
        },
        "required": ["indices", "reason"],
    })

    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}

    result = subprocess.run(
        [
            "claude", "-p", prompt,
            "--model", LLM_MODEL,
            "--output-format", "json",
            "--json-schema", schema,
            "--max-turns", "2",
        ],
        capture_output=True, text=True, timeout=LLM_TIMEOUT,
        env=env,
    )

    parsed = json.loads(result.stdout)
    structured = parsed["structured_output"]
    indices = structured["indices"]
    reason = structured.get("reason", "")

    valid = [i for i in indices if 0 <= i < len(candidates)]
    if not valid:
        return list(range(min(n, len(candidates)))), "LLM returned no valid indices, using score order"

    return valid[:n], reason


def _probe_and_select(
    query: str,
    scored: list[ScoredResult],
    narrator_pref: str | None = None,
) -> tuple[ScoredResult, int, dict[str, int], str]:
    """LLM picks top candidates from all results, probe those for peers, pick best. Returns (chosen, peers, counts, reason)."""
    reason = ""
    llm_picks: list[ScoredResult] = []

    # Step 1: LLM ranks all candidates, picks top N
    try:
        indices, reason = _llm_pick_top(query, scored, narrator_pref=narrator_pref)
        llm_picks = [scored[i] for i in indices]
        _log(f"  LLM ranked: {indices} — {reason}")
    except Exception as e:
        _log(f"  LLM selection failed ({e}), using score order")
        llm_picks = scored[:PROBE_CANDIDATES]
        reason = "heuristic fallback"

    # Step 2: Probe LLM's picks for peers
    magnets = [s.result.magnet for s in llm_picks]
    peer_counts = _probe_seeds_batch(magnets)

    # Step 3: Pick highest LLM-ranked with peers, fallback to LLM's #1
    chosen = next(
        (s for s in llm_picks if peer_counts.get(s.result.magnet, 0) > 0),
        llm_picks[0],
    )

    return chosen, peer_counts.get(chosen.result.magnet, 0), peer_counts, reason


def _direct_abb_search(query: str) -> list[SearchResult]:
    """Search ABB directly with a raw query string (no Open Library metadata)."""
    _log(f'  Searching ABB directly: "{query}"')
    return list(abb.search(query, max_pages=1))


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
    bg: bool = False,
) -> None:
    _log(f"\n  Looking up: {query}")

    book: BookMetadata | None = None
    llm_narrator: str | None = None

    # Step 1: LLM parses the freeform query into structured metadata
    try:
        book, llm_narrator = _llm_parse_query(query)
        if llm_narrator and not narrator_pref:
            narrator_pref = llm_narrator
    except Exception as e:
        _log(f"  LLM query parse failed ({e}), searching ABB directly...")

    if book:
        _log(f"\n  Title:  {book.title}")
        if book.author:
            _log(f"  Author: {book.author}")
        if book.series:
            _log(f"  Series: {book.series}")
        if narrator_pref:
            _log(f"  Narrator: {narrator_pref}")
        _log("")

    # Step 2: Try OL for cover image only (non-blocking)
    cover_id: int | None = None
    if book:
        try:
            ol_book = lookup_book(book.title)
            if ol_book:
                cover_id = ol_book.cover_id
        except Exception:
            pass

    # Step 3: Search ABB with fan-out queries
    try:
        if book:
            results = _fan_out_search(book, raw_query=query)
        else:
            results = _direct_abb_search(query)
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
    if book and len(results) > max_enrich:
        results.sort(key=lambda r: quick_score(r, book), reverse=True)
        results = results[:max_enrich]
    elif not book and len(results) > max_enrich:
        results = results[:max_enrich]

    _log(f"\n  Found {len(results)} candidates, loading details...")
    results = _enrich_results(results)

    if book:
        scored = score_and_sort(results, book, narrator_pref, MIN_SCORE)
    else:
        scored = [ScoredResult(result=r, score=50) for r in results if r.magnet]

    if not scored:
        if json_mode:
            _json_error("No good matches found")
        else:
            print("  No good matches found.")
        return

    scored = scored[:limit]
    if book and book.author:
        display_title = f"{book.title} - {book.author}"
    elif book:
        display_title = book.title
    else:
        display_title = query

    def _action(title: str, magnet: str, cid: int | None) -> dict | None:
        if stream_mode:
            return _execute_stream(magnet, json_mode, player, port, keep)
        if bg:
            return _execute_download_bg(title, magnet, cid)
        return _execute_download_fg(title, magnet, cid)

    action_word = "Stream" if stream_mode else "Download"

    if json_mode:
        book_dict = asdict(book) if book else {"title": query, "author": "", "year": None, "series": None, "cover_id": None}
        if cover_id and book:
            book_dict["cover_id"] = cover_id
        if narrator_pref:
            book_dict["narrator_pref"] = narrator_pref
        output: dict = {
            "book": book_dict,
            "results": [_scored_to_dict(s) for s in scored],
            "download": None,
        }
        if auto:
            chosen, peers, peer_counts, reason = _probe_and_select(query, scored, narrator_pref)
            output["download"] = _action(display_title, chosen.result.magnet, cover_id)
            output["selected_index"] = scored.index(chosen)
            output["peers"] = peers
            output["reason"] = reason
            if not any(v > 0 for v in peer_counts.values()):
                output["warning"] = "No results had active peers, using top match"
        _json_out(output)
        return

    if auto:
        _log(f"\n  Selecting from {len(scored)} results...")
        chosen, peers, _, reason = _probe_and_select(query, scored, narrator_pref)
        if peers > 0:
            print(f"\n  Selected: {chosen.result.title} ({chosen.score}% match, {peers} peer(s))")
        else:
            print(f"\n  No peers found, trying top match: {chosen.result.title}")

        print()
        _action(display_title, chosen.result.magnet, cover_id)
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

    _action(display_title, selected.result.magnet, cover_id)


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
            if info["status"] == "completed":
                print(f"\n  Download complete: {info['path']}")
            elif info["status"] == "interrupted":
                print("\n  Download interrupted (resume data saved)")
            else:
                print(f"\n  Download {info['status']}: {info.get('error', 'unknown error')}")


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
            print(f"  ID:       {state['id']}")
            print(f"  Title:    {state['title']}")
            print(f"  Status:   {state['status']}")
            if "progress" in state and state["status"] == "downloading":
                print(f"  Progress: {state['progress'] * 100:.1f}%")
            print(f"  Path:     {state['path']}")
            if state.get("cover"):
                print(f"  Cover:    {state['cover']}")
            print(f"  Started:  {state['started_at']}")
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
            progress = ""
            if s["status"] == "downloading" and "progress" in s:
                progress = f" ({s['progress'] * 100:.0f}%)"
            print(f"  {status_icon} [{s['id']}] {s['title']} — {s['status']}{progress}")


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
    except (StreamError, TorrentError) as e:
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

def _build_parser(prog: str = "auto-torrent", default_source: str = "abb") -> argparse.ArgumentParser:
    if prog == "atb":
        description = "Search AudiobookBay and download audiobooks via libtorrent."
    elif prog == "atv":
        description = "Search The Pirate Bay and download movies/TV via libtorrent."
    else:
        description = "Search AudiobookBay / The Pirate Bay and download via libtorrent."

    parser = argparse.ArgumentParser(prog=prog, description=description)

    subs = parser.add_subparsers(dest="command")

    # search
    search_p = subs.add_parser("search", help="Search for a torrent")
    search_p.add_argument("query", nargs="*", help="Title to search for")
    search_p.add_argument("--json", action="store_true", help="Output structured JSON")
    search_p.add_argument("--auto", action="store_true", help="Auto-select best match and download")
    search_p.add_argument("--narrator", nargs="+", help="Preferred narrator name (ABB only)")
    search_p.add_argument(
        "--source", choices=["abb", "tpb"], default=default_source,
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
    search_p.add_argument("--bg", action="store_true", help="Download in background (with --auto)")
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
    stream_p = subs.add_parser(
        "stream",
        help="Stream a magnet link (use 'search --stream' to search and stream)",
    )
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


def main_atb() -> None:
    """Entry point for atb (auto torrent book) - audiobook-specific interface."""
    parser = _build_parser(prog="atb", default_source="abb")
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


def main_atv() -> None:
    """Entry point for atv (auto torrent video) - movie/TV-specific interface."""
    parser = _build_parser(prog="atv", default_source="tpb")
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
