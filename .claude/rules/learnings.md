---
name: auto-torrent-learnings
description: Session-specific learnings for auto-torrent project
---

# Session Learnings

- [2026-02-13] (2x) ABB (audiobookbay.lu) rate limits aggressively after ~30 requests in short window — throttles to 0 results. Space requests or add retry delays (5-10s backoff)
- [2026-02-13] (1x) Fuzzy matching: rapidfuzz is drop-in for fuzzywuzzy, supports partial token-sort, faster. Use for title/author/series deduplication
- [2026-02-13] (1x) Open Library API: free, no auth, returns author + series + cover image IDs. Subjects field contains series name (parse carefully, some noise)
- [2026-02-13] (1x) Scorer design: title 50% + author 30% + series 20% baseline, narrator flag adds ±5-15% to individual results. Capped at 100%, penalizes missing narrator when flag set
- [2026-02-13] (1x) --narrator flag logic: when narrator specified, require it to be highest match — title+author+series alone insufficient. Penalizes no-narrator results by 5%, exact match gets +15%
- [2026-02-13] (1x) aria2c already integrated: download() function wired, --seed-time=0 prevents seeding, --bt-stop-timeout=300 kills if stalled 5min
- [2026-02-13] (1x) Interactive vs auto mode: interactive = show picker; auto = --auto flag picks top match automatically, downloads cover + audio to ~/Downloads/audiobooks/<title>/
- [2026-02-13] (1x) Basil integration: JSON CLI output mode cleaner than MCP server or ports/adapters. Agent shells out to `uv run auto-torrent --json`, gets structured results to reason about
- [2026-02-13] (1x) Agent UX blocker: monolithic CLI prevents conversational flow (search → show results → user picks one → download that specific result). Subcommands solve this: `auto-torrent search ...` + `auto-torrent download <magnet> ...`
- [2026-02-13] (1x) Top feature gaps for agent flow: (1) rate-limit error messages so agent knows why it failed, (2) --limit N to reduce noise, (3) --background + status polling for non-blocking downloads
- [2026-02-13] (1x) Subcommands live: `auto-torrent search`, `download <magnet> --title`, `status [id]`. Background downloads use `Popen`, PID liveness check for status, state files in `~/.auto-torrent/downloads/`
- [2026-02-13] (1x) ABB connection errors now surfaced: custom `ABBError` exception + pretty CLI output ("is not responding", "is unreachable", "returned an error"). JSON mode returns `{"error": "..."}` for agent reasoning
- [2026-02-13] (1x) Rate limiting tactics implemented in order of impact: (1) random delays 1.5-3s between ABB requests (biggest win), (2) requests.Session with 10-conn pool (reuse TCP), (3) retry + backoff on 429/5xx (exponential, 3x), (4) requests-cache SQLite 1hr expiry (local caching), (5) pre-filter candidates before detail fetch (--limit 5 now enriches ~10 not 50+)
- [2026-02-13] (1x) ABB site reliability: goes offline periodically (connection timeouts). Code gracefully degrades with error messages. When back up, new protections prevent throttling during normal usage
- [2026-02-13] (1x) !! BitTorrent trackers age poorly — openbittorrent.com, opentor, ccc.de, coppersurfer, leechers-paradise all dead. Use ngosang/trackerslist for current active list, rotate periodically
- [2026-02-13] (1x) DHT/PEX/LPD essential for modern torrent connectivity — `--enable-dht=true`, `--enable-pex=true`, `--enable-lsd=true` in aria2c. Trackers alone insufficient, DHT bootstraps via fallback peers
- [2026-02-13] (1x) Torrent quality scoring: seeders (log-scale 0-30pts), source (25), resolution (20), uploader trust (15), codec (5), HDR (5). Parse title regex for resolution/source/codec/HDR. Distance-based resolution preference: preferred=20pts, ±1 step=12, ±2=5, ±3=2
- [2026-02-13] (1x) TPBResult dataclass frozen, prevents round-trip format→parse. Store raw `size_bytes: int` not formatted string. Compute warnings at search time, not display time
- [2026-02-13] (1x) TPB safety features: category filter, min_seeds threshold, size warnings (show in results), post-download suspicious extension scan (.exe/.bat/.msi/.ps1 etc)
- [2026-02-13] (1x) aria2c `CN:0 SD:0` = zero connections/seeds — indicates tracker failures, not peer absence. Fix trackers or enable DHT/PEX/LPD
- [2026-02-13] (1x) !! iina-cli has known bug with HTTP URL parsing (GitHub #3688) — shows "URL invalid" + auth dialog. Use `open "iina://weblink?url=<url>"` instead of iina-cli CLI, returns immediately (fire-and-forget)
- [2026-02-13] (1x) iina fire-and-forget launch via `open` — player_proc returns None, need to handle in main loop. Don't track iina's PID
- [2026-02-13] (1x) HTTP server handling iina seeks/closes: ConnectionResetError floods stderr. Create custom `_QuietHTTPServer(ThreadingHTTPServer)` with no-op `handle_error()` to suppress
- [2026-02-13] (1x) iina streaming buffering: add `--mpv-cache=yes --mpv-demuxer-max-bytes=50M --mpv-network-timeout=30` flags for HTTP Range stream playback, prevents "perma loading"
- [2026-02-13] (1x) Code simplification wins: remove section header comments (file already small), obvious docstrings (repeat function name), redundant guards, inline single-use vars. stream.py 551→505 lines (-46)
- [2026-02-13] (1x) !! TitleInfo NamedTuple pattern: `parse_title()` → typed return enables attribute access in callers, prevents silent key-miss crashes
- [2026-02-13] (1x) TPB API response handling: guard with `try/except (ValueError, TypeError)` around `int()` conversions — malformed items silently skipped
- [2026-02-13] (1x) TPB scoring constants consolidated: moved `_SOURCE_SCORES`, `_STATUS_SCORES`, `_RES_LADDER`, distance map, codec scores to config.py with `TPB_` prefix. Pattern enables testing and reuse
- [2026-02-13] (1x) stream.py MIME type: use `mimetypes.guess_type()` fallback to "video/mp4" — supports .mkv/.avi/.webm, not just MP4
- [2026-02-13] (1x) Process death detection: check for files in download directory (`path.exists() and any(path.iterdir())`) not just PID. Empty dir = failed, files = completed
