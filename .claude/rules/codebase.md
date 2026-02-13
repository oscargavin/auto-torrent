# Codebase Knowledge

## Map
- cli.py: main, arg parsing, dual source path (ABB for audiobooks, TPB for video). Subcommands: `search --source {abb|tpb}`, `download <magnet> --title`, `stream <magnet> --player {mpv|iina|vlc}`, `status [id]`. Search/download/stream support `--stream` flag for integrated workflow. `_resolve_status()` checks for files in download directory
- tpb.py: `TPBResult` dataclass (frozen, with score/warning fields), `parse_title()` → `TitleInfo` NamedTuple, `score_result()` weighted scorer imports constants from config, guarded malformed API responses with try/except
- abb.py: `search_abb()` (fan-out ABB + parallel fetch_details), `ABBError` exception for connection/HTTP errors
- lib.py: `search_open_library()` (metadata), `score_results()` (fuzzy match + narrator weighting)
- download.py: `download()` (aria2c invoke + cover fetch), `_execute_download_fg/bg()` (foreground blocking vs background Popen)
- stream.py: `stream()` orchestrator, libtorrent2 session + sequential piece prioritization, HTTP Range server via `_make_handler()`, player detection + launch (`mpv`/`iina`/`vlc`), iina uses `open iina://weblink?url=` scheme. ~505 lines after cleanup
- config.py: `STATE_DIR` (~/.auto-torrent/downloads/), `STREAM_DIR` (~/.auto-torrent/stream), `CACHE_DIR`, `DEFAULT_LIMIT`, `DEFAULT_TRACKERS` (6 active UDP trackers), `STREAM_BUFFER_MB`, `STREAM_PORT`. TPB scoring constants: `TPB_SOURCE_SCORES`, `TPB_STATUS_SCORES`, `TPB_RESOLUTION_LADDER`, `TPB_RESOLUTION_DISTANCE_SCORES`, `TPB_CODEC_SCORES`, `MAX_SEED_SCORE`, `SEED_LOG_SCALE`
- Background state: JSON files in STATE_DIR track PID, magnet, title, status for `cmd_status` polling

## Navigation
- Open Library: `/search.json?title/author` → parse results for metadata, cover ID
- ABB: shared `requests.Session` (10-conn pool), 1.5-3s delays between requests, retry backoff on 429/5xx, requests-cache 1hr
- Error handling: `ABBError` raised on timeout/connection/HTTP errors, caught in `cmd_search` and formatted prettily in CLI + JSON
- Scoring: `quick_score()` (title+author only, pre-filter), `score_results()` (full: 50% title + 30% author + 20% series, narrator ±5-15%), `score_result()` (TPB: seeders/source/resolution/trust/codec/HDR, imports constants from config.py)
- Background tracking: state files (JSON) in `~/.auto-torrent/downloads/`, PID liveness check via `os.kill(pid, 0)`. Status detection: `_resolve_status()` differentiates completed vs failed by checking for files in download directory, not just process death
- Streaming: libtorrent2 session in `stream.py`, HTTP server on configurable port, sequential piece prioritization for playback, MP4 moov atom pre-prioritized
- Player launch: `_detect_player()` tries mpv → iina → vlc, `_launch_player()` handles fire-and-forget iina (via `open iina://weblink?url=`) vs tracked Popen (mpv/vlc)

## Reusables
- rapidfuzz: for fuzzy matching (title/author/series dedup) — installed via uv
- aria2c: binary (installed via brew), called as subprocess

## Recipes
- ABB search: `cmd_search() --source abb` → Open Library lookup → ABB fan-out + parallel detail fetch → pre-filter quick_score() → full score_results() → sort → pick/auto → cmd_download()
- TPB search: `cmd_search() --source tpb --category {video|...} --min-seeds 5 --quality 1080p` → parse_title() on each result → score_result() weighted → sorted desc → pick/auto → cmd_download()
- Stream flow: `search ... --stream` or `stream <magnet> --player iina` → libtorrent2 session → sequential download + HTTP server → player launch (mpv via Popen, iina via `open` URL scheme) → status loop until Ctrl+C or complete
- Download flow: `cmd_download(<magnet> --title "...")` → if `--bg`: Popen aria2c (DHT+PEX+LPD), write state, return ID. Else: blocking, wait, show path
- Status flow: `cmd_status([id])` → read state files → PID liveness check → resolve (downloading/completed/failed)
- Narrator weighting: when `--narrator "name"` passed, exact match +15%, missing -5%, wrong narrator no bonus
- TPB scoring layers: seeders log-scale (0-30), source quality (25), resolution preference (20), uploader trust (15), codec (5), HDR (5)
- Code cleanup: remove section header comments (not needed for <600-line files), obvious docstrings (repeat function name), redundant guards (already checked by caller)
- Basil integration: `uv run auto-torrent search --json --limit 3` → agent picks → `uv run auto-torrent download <magnet> --title "..." --json --bg` → agent polls `uv run auto-torrent status <id> --json`

## Fragile
- ABB HTML scraping: site layout changes break format/bitrate/narrator parsing. Site also goes offline periodically (timeouts)
- TPB title parsing: regex assumptions for resolution/source/codec. Site layout changes break scraping. Live test needed after any updates. API responses guarded with try/except, malformed items skipped
- BitTorrent trackers: age poorly, need periodic refresh from ngosang/trackerslist. DHT/PEX/LPD essential fallback
- Requests library SSL/connection pooling: ensure Session is scoped correctly (reused across searches in one process)
- Background process tracking: PID files in STATE_DIR may orphan if process dies hard. No garbage collection yet (manual cleanup needed)
- Cache invalidation: requests-cache default 1hr expiry; if sources change format, cached responses stale until expiry
- Open Library subject parsing: series buried in subjects array, inconsistent format (rapidfuzz token-sort match needed)
- iina integration: fire-and-forget launch via `open` returns immediately. Player doesn't connect if macOS Local Network permission denied (Settings > Privacy & Security > Local Network). HTTP Range server needs _QuietHTTPServer (suppress handle_error) to avoid Connection Reset spam
