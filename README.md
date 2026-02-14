# auto-torrent

CLI tool that searches for torrents and downloads them via libtorrent. Supports AudiobookBay (audiobooks with fuzzy metadata matching) and The Pirate Bay (general torrents).

## Install

Requires Python 3.12+.

```bash
# Development (editable install)
uv tool install --editable .

# Or just dependencies (requires uv run prefix)
uv sync
```

## Usage

Three entry points:
- **`atb`** (auto torrent book) — audiobooks via AudiobookBay (default source)
- **`atv`** (auto torrent video) — movies/TV via The Pirate Bay (default source)
- **`auto-torrent`** — generic interface (specify `--source`)

### Search

```bash
# Audiobooks (atb) — interactive picker
atb search "project hail mary"

# Auto-select best match and download immediately
atb search "the name of the wind" --auto

# JSON output for scripts/agents
atb search "the name of the wind" --json --limit 5

# Prefer a specific narrator
atb search "the wise mans fear" --narrator Rupert Degas --json

# Movies/TV (atv) — The Pirate Bay
atv search "interstellar 2014 1080p"

# TPB with JSON output
atv search "interstellar" --json --limit 5

# Override source (using generic auto-torrent)
auto-torrent search "something" --source tpb
```

### Download

```bash
# Foreground — blocks until libtorrent finishes
atb download "magnet:?xt=urn:btih:..." --title "Project Hail Mary"

# Background — returns immediately with a download ID
atb download "magnet:?xt=urn:btih:..." --title "Project Hail Mary" --bg --json

# With cover art from Open Library
atb download "magnet:?xt=urn:btih:..." --title "Project Hail Mary" --cover-id 12345 --json
```

### Status

```bash
# List all downloads
atb status

# Check a specific download
atb status a1b2c3d4 --json
```

### Stream

```bash
# Stream to default player (mpv/iina/vlc)
atv stream "magnet:?xt=urn:btih:..."

# Specify player
atv stream "magnet:?xt=urn:btih:..." --player iina
```

## How It Works

### AudiobookBay (atb)

1. **LLM query parsing** — extracts title, author, narrator from freeform query
2. **Open Library lookup** — resolves to canonical metadata (title, author, series, cover ID)
3. **ABB fan-out** — searches AudiobookBay with title, series, and author queries in parallel
4. **Detail enrichment** — scrapes narrator, format, bitrate from each result page (concurrent workers)
5. **DHT peer probing** — uses libtorrent to probe top 3 results for live seeders (early exit at first peer)
6. **LLM disambiguation** — Sonnet picks best match when scores are tied
7. **Download** — libtorrent with sequential piece mode for large files, auto-resume support

### The Pirate Bay (atv)

1. **API query** — searches apibay.org JSON API (no scraping, no Cloudflare)
2. **Title parsing** — extracts resolution, source, codec, HDR from title
3. **Scoring** — weighted by seeders (log-scale), source quality, resolution, uploader trust, codec
4. **Download** — same libtorrent pipeline as atb

Downloads go to `~/.auto-torrent/downloads/<title>/`.

## Agent Integration

Designed for use by AI agents via `--json` flag. Typical flow:

```bash
# 1. Search and get structured results
atb search "project hail mary" --json --limit 3

# 2. Agent picks a result, downloads it
atb download "magnet:?xt=..." --title "Project Hail Mary" --bg --json

# 3. Poll for completion
atb status a1b2c3d4 --json
```

All JSON output goes to stdout. Errors return `{"error": "message"}`.

## License

MIT
