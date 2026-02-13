# auto-torrent

CLI tool that searches for torrents and downloads them via aria2. Supports AudiobookBay (audiobooks with fuzzy metadata matching) and The Pirate Bay (general torrents).

## Install

Requires Python 3.12+ and [aria2](https://aria2.github.io/).

```bash
brew install aria2  # macOS
uv sync             # install Python dependencies
```

## Usage

### Search

```bash
# AudiobookBay (default) — interactive picker
auto-torrent search "project hail mary"

# Auto-select best match and download immediately
auto-torrent search "the name of the wind" --auto

# JSON output for scripts/agents
auto-torrent search "the name of the wind" --json --limit 5

# Prefer a specific narrator
auto-torrent search "the wise mans fear" --narrator Rupert Degas --json

# The Pirate Bay — general torrents (movies, TV, software, etc.)
auto-torrent search "interstellar 2014 1080p" --source tpb

# TPB with JSON output
auto-torrent search "interstellar" --source tpb --json --limit 5
```

### Download

```bash
# Foreground — blocks until aria2c finishes
auto-torrent download "magnet:?xt=urn:btih:..." --title "Project Hail Mary"

# Background — returns immediately with a download ID
auto-torrent download "magnet:?xt=urn:btih:..." --title "Project Hail Mary" --bg --json

# With cover art from Open Library
auto-torrent download "magnet:?xt=urn:btih:..." --title "Project Hail Mary" --cover-id 12345 --json
```

### Status

```bash
# List all downloads
auto-torrent status

# Check a specific download
auto-torrent status a1b2c3d4 --json
```

## How It Works

### AudiobookBay (`--source abb`, default)

1. **Open Library lookup** — resolves the query to canonical title, author, series, and cover ID
2. **ABB fan-out** — searches AudiobookBay with title, series, and author queries in parallel
3. **Detail enrichment** — scrapes narrator, format, bitrate from each result page (concurrent workers)
4. **Scoring** — fuzzy matches each result against canonical metadata (title 50%, author 30%, series 20%) with narrator preference bonus
5. **Download** — hands the magnet to aria2c with auto-stop on completion (`--seed-time=0`)

### The Pirate Bay (`--source tpb`)

1. **API query** — searches apibay.org JSON API (no scraping, no Cloudflare)
2. **Results** — returns title, size, seeders, category, and magnet link
3. **Download** — same aria2c pipeline as ABB

Downloads go to `~/Downloads/audiobooks/<title>/`.

## Agent Integration

Designed for use by AI agents via `--json` flag. Typical flow:

```bash
# 1. Search and get structured results
uv run auto-torrent search "project hail mary" --json --limit 3

# 2. Agent picks a result, downloads it
uv run auto-torrent download "magnet:?xt=..." --title "Project Hail Mary" --bg --json

# 3. Poll for completion
uv run auto-torrent status a1b2c3d4 --json
```

All JSON output goes to stdout. Errors return `{"error": "message"}`.

## License

MIT
