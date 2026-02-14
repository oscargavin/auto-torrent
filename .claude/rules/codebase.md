# Codebase Knowledge

## Map
- cli.py: three entry points via `_build_parser(prog, default_source)`: `main()` generic, `main_atb()` (AudiobookBay default, atb command), `main_atv()` (TPB default, atv command). Registered in pyproject.toml [project.scripts]. Subcommands: `search [--source {abb|tpb}]`, `download <magnet> --title`, `stream <magnet> --player {mpv|iina|vlc}`, `status [id]`. Search/download/stream support `--stream` flag. `_probe_seeds_batch()` probes N magnets in parallel via shared libtorrent session for peer discovery. `_probe_and_select()` probes + LLM-selects best result (falls back to heuristic). `_llm_select()` calls `claude -p --json-schema` with Sonnet to disambiguate results. `_resolve_status()` checks for files in download directory
- torrent.py: shared libtorrent primitives — `create_session()`, `add_magnet()` (supports sequential flag), `wait_for_metadata()`, `format_speed()`, `TorrentError` exception. Used by both stream.py and download.py
- download.py: `download_torrent()` (libtorrent-based, parallel piece download, progress reporting, resume support), `run_background_download()` (entry point for `multiprocessing.Process`). Replaces aria2c
- tpb.py: `TPBResult` dataclass (frozen, with score/warning fields), `parse_title()` → `TitleInfo` NamedTuple, `score_result()` weighted scorer imports constants from config, guarded malformed API responses with try/except
- abb.py: `search_abb()` (fan-out ABB + parallel fetch_details), `ABBError` exception for connection/HTTP errors
- lib.py: `search_open_library()` (metadata), `score_results()` (fuzzy match + narrator weighting)
- stream.py: `stream()` orchestrator, imports libtorrent helpers from `torrent.py`, sequential piece prioritization, HTTP Range server via `_make_handler()`, player detection + launch (`mpv`/`iina`/`vlc`), iina uses `open iina://weblink?url=` scheme
- config.py: `STATE_DIR` (~/.auto-torrent/downloads/), `STREAM_DIR` (~/.auto-torrent/stream), `CACHE_DIR`, `DEFAULT_LIMIT`, `DEFAULT_TRACKERS` (8 active UDP trackers), `STREAM_BUFFER_MB`, `STREAM_PORT`, `DHT_BOOTSTRAP_NODES` (3 entry points), `PROBE_TIMEOUT` (15s), `PROBE_CANDIDATES` (3), `LLM_MODEL` (sonnet), `LLM_TIMEOUT` (30s), `DOWNLOAD_PROGRESS_INTERVAL` (5s). TPB scoring constants: `TPB_SOURCE_SCORES`, `TPB_STATUS_SCORES`, `TPB_RESOLUTION_LADDER`, `TPB_RESOLUTION_DISTANCE_SCORES`, `TPB_CODEC_SCORES`, `MAX_SEED_SCORE`, `SEED_LOG_SCALE`. Proxy config: `get_proxy()` reads `~/.auto-torrent/config.json` → `proxy_url` field; also checks `AUTO_TORRENT_PROXY` env var
- Background state: JSON files in STATE_DIR track PID, magnet, title, status, progress for `cmd_status` polling

## Navigation
- Open Library: `/search.json?q=...` (q field, not title) → `_clean_query()` strips audiobook noise/trailing digits first → `_query_variations()` tries original, strip articles, strip subtitle → `lookup_book()` returns None if all fail → ABB direct fallback in `cmd_search()`. Series subjects may have `franchise:` prefix (strip it)
- ABB: shared `requests.Session` (10-conn pool), 1.5-3s delays between requests, retry backoff on 429/5xx, requests-cache 1hr. Direct search (no OL) skips detail page fetching, returns baseline 50% score
- Error handling: `ABBError` raised on timeout/connection/HTTP errors, `TorrentError` from torrent.py for libtorrent failures. Top-level try/except in cmd_search/download/stream catches uncaught exceptions → JSON `{"error": "..."}` or stderr
- Scoring: `quick_score()` (title+author only, pre-filter), `score_results()` (full: 50% title + 30% author + 20% series, narrator ±5-15%), `score_result()` (TPB: seeders/source/resolution/trust/codec/HDR, imports constants from config.py)
- Background tracking: state files (JSON) in `~/.auto-torrent/downloads/`, PID liveness check via `os.kill(pid, 0)`. Status detection: `_resolve_status()` differentiates completed vs failed by checking for files in download directory. Progress field (0.0-1.0) updated every 5s by background libtorrent process
- Streaming: imports from `torrent.py` (create_session, add_magnet, wait_for_metadata), HTTP server on configurable port, sequential piece prioritization for playback, MP4 moov atom pre-prioritized
- Downloading: `download.py` uses `torrent.py` helpers, parallel piece mode (not sequential), progress reporting. Background downloads via `multiprocessing.Process`, saves resume data on shutdown
- LLM selection: `_llm_select()` calls `claude -p --json-schema` with Sonnet model, timeout 30s. Formats candidates with title/narrator/format/size/peers/description. Falls back to peer-count heuristic on failure
- Player launch: `_detect_player()` tries mpv → iina → vlc, `_launch_player()` handles fire-and-forget iina (via `open iina://weblink?url=`) vs tracked Popen (mpv/vlc)
- Proxy: `cli.py --proxy flag` → threaded to `search_abb()`, `search_tpb()`, `stream()`. Config fallback in `config.get_proxy()`. PySocks dependency enables `socks5h://` and `http://` URLs

## Reusables
- rapidfuzz: for fuzzy matching (title/author/series dedup) — installed via uv
- libtorrent: BitTorrent library for both streaming and downloading (replaced aria2c)
- claude CLI: used for LLM result disambiguation via `claude -p --json-schema`

## Recipes
- ABB search: `cmd_search() --source abb` → OL lookup: clean query (strip noise/digits) → try variations (articles/subtitle) → if OL succeeds: ABB fan-out + parallel detail fetch, else: ABB direct search (skip detail) → pre-filter quick_score() → full score_results() → sort → **parallel DHT peer probe on top 3** → **LLM selects best match** (fallback: first-with-peers → top) → cmd_download()
- TPB search: `cmd_search() --source tpb --category {video|...} --min-seeds 5 --quality 1080p` → parse_title() on each result → score_result() weighted → sorted desc → pick/auto → cmd_download()
- Stream flow: `search ... --stream` or `stream <magnet> --player iina` → torrent.py session → sequential download + HTTP server → player launch → status loop until Ctrl+C or complete
- Download flow: `cmd_download(<magnet> --title "...")` → if `--bg`: multiprocessing.Process(run_background_download), write state, return ID. Else: blocking libtorrent download, Ctrl+C saves resume data
- Status flow: `cmd_status([id])` → read state files → PID liveness check → resolve (downloading/completed/failed). Shows progress % for active downloads
- Narrator weighting: when `--narrator "name"` passed, exact match +15%, missing -5%, wrong narrator no bonus
- TPB scoring layers: seeders log-scale (0-30), source quality (25), resolution preference (20), uploader trust (15), codec (5), HDR (5)
- Error handling: cmd_search/download/stream wrap payload in try/except → JSON mode outputs `{"error": "type: msg"}`, non-JSON stderr + exit 1
- Basil integration: `uv run atb search --json --limit 3` → agent picks → `uv run atb download <magnet> --title "..." --json --bg` → agent polls `uv run atb status <id> --json`
- Basil MCP tools: search_audiobooks, download_audiobook, check_download_status in basil/src/agent/tools/audiobooks.ts. Shell out via execFile with 120s timeout (use atb command)

## Fragile
- ABB HTML scraping: site layout changes break format/bitrate/narrator parsing. Site goes offline periodically (timeouts). Direct fallback search (no OL) skips detail fetch, reduces load but returns baseline score
- TPB title parsing: regex assumptions for resolution/source/codec. Site layout changes break scraping. Live test needed after any updates. API responses guarded with try/except, malformed items skipped
- BitTorrent trackers: age poorly, need periodic refresh from ngosang/trackerslist. DHT/PEX/LPD essential fallback. 8 current trackers in config.py
- Open Library query matching: noise words ("graphic audio", "unabridged", trailing volume numbers) must be stripped before variations. Subjects have `franchise:` prefix (strip it). "Wise Man's Fear" fails (missing article), "The Wise Man's Fear" succeeds
- LLM selection: depends on `claude` CLI being installed and available in PATH. Unsets CLAUDE_CODE/CLAUDECODE env vars to allow nesting. 30s timeout → heuristic fallback. JSON parsing of stdout required
- iina integration: fire-and-forget launch via `open` returns immediately. Player doesn't connect if macOS Local Network permission denied (Settings > Privacy & Security > Local Network). HTTP Range server needs _QuietHTTPServer (suppress handle_error) to avoid Connection Reset spam
