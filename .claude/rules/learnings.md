---
name: auto-torrent-learnings
description: Session-specific learnings for auto-torrent project
---

# Session Learnings

- [2026-02-13] (1x) ABB (audiobookbay.lu) rate limits aggressively after ~30 requests in short window — throttles to 0 results. Space requests or add retry delays (5-10s backoff)
- [2026-02-13] (1x) Fuzzy matching: rapidfuzz is drop-in for fuzzywuzzy, supports partial token-sort, faster. Use for title/author/series deduplication
- [2026-02-13] (1x) Open Library API: free, no auth, returns author + series + cover image IDs. Subjects field contains series name (parse carefully, some noise)
- [2026-02-13] (1x) Scorer design: title 50% + author 30% + series 20% baseline, narrator flag adds ±5-15% to individual results. Capped at 100%, penalizes missing narrator when flag set
- [2026-02-13] (1x) --narrator flag logic: when narrator specified, require it to be highest match — title+author+series alone insufficient. Penalizes no-narrator results by 5%, exact match gets +15%
- [2026-02-13] (1x) aria2c already integrated: download() function wired, --seed-time=0 prevents seeding, --bt-stop-timeout=300 kills if stalled 5min
- [2026-02-13] (1x) Interactive vs auto mode: interactive = show picker; auto = --auto flag picks top match automatically, downloads cover + audio to ~/Downloads/audiobooks/<title>/
- [2026-02-13] (1x) Basil integration: JSON CLI output mode cleaner than MCP server or ports/adapters. Agent shells out to `uv run auto-torrent --json`, gets structured results to reason about
- [2026-02-13] (1x) Agent UX blocker: monolithic CLI prevents conversational flow (search → show results → user picks one → download that specific result). Subcommands solve this: `auto-torrent search ...` + `auto-torrent download <magnet> ...`
- [2026-02-13] (1x) Top feature gaps for agent flow: (1) rate-limit error messages so agent knows why it failed, (2) --limit N to reduce noise, (3) --background + status polling for non-blocking downloads
