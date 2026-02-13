# Codebase Knowledge

## Map
- main.py: entry point, CLI argument parsing (--json, --auto, --limit, --narrator), dispatches to search/download subcommands
- search_open_library(): queries Open Library for canonical book metadata (title, author, series)
- search_abb(): fan-out to ABB with title/series/author, returns list of results (title, narrator, format, bitrate, size, magnet)
- fetch_details(): parallel worker for ABB detail pages (fetches narrator, format, bitrate from HTML)
- score_results(): fuzzy match against canonical + narrator preference weighting
- download(): aria2c invocation + cover image fetch from Open Library
- _execute_download(): internal helper, returns path + cover location for JSON output

## Navigation
- Open Library queries: `/search.json?title=...&author=...`
- ABB search: GET to search page, parse HTML result rows
- ABB detail: GET result URL, scrape narrator/format/bitrate from page
- aria2c: called with `--seed-time=0 --bt-stop-timeout=300`
- Cover images: Open Library `/api/works/` returns ISBN → cover ID → CDN URL

## Reusables
- rapidfuzz: for fuzzy matching (title/author/series dedup) — installed via uv
- aria2c: binary (installed via brew), called as subprocess

## Recipes
- Full search flow: user input → Open Library canonical lookup → ABB fan-out (3 queries) → parallel detail fetch with progress → fuzzy score → sort by score → show picker → download on confirm
- Auto mode: same, but skip picker, auto-select rank 1 result
- Narrator preference: pass `--narrator "name"` → scorer adds +15 on exact match, -5 on missing
- Cover download: extract ISBN from Open Library response → fetch from CDN
- Basil integration: `uv run auto-torrent --json [--auto] [--narrator "..."] [--limit N] <query>` → returns `{book, results, download}` JSON for agent parsing
- Subcommand pattern (planned): `auto-torrent search` (returns results) → agent picks → `auto-torrent download <magnet>` (downloads specific result) → `auto-torrent status <id>` (checks progress)

## Fragile
- ABB HTML scraping: fragile to layout changes. Format/bitrate/narrator parsing may break if page HTML shifts
- ABB rate limiting: ~30 requests in short window → 0 results silently. Agent can't distinguish failure from empty results. Need error message field in JSON output
- Open Library series detection: buried in `subjects` array — not always present or clearly formatted. May need fuzzy match against known series
- Scorer narrator logic: when --narrator specified, title+author+series alone insufficient (86% match). Need exact narrator match to reach 100%. Current: exact +15%, wrong +5%, missing -5%
- Subcommand refactor will split monolithic main logic — ensure test coverage for each subcommand path before merge
