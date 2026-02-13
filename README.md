# auto-torrent

CLI tool that searches AudiobookBay for audiobooks and downloads them via aria2. Looks up canonical metadata from Open Library, fuzzy-matches results, and scores them by title/author/series/narrator fit.

## Install

Requires Python 3.12+ and [aria2](https://aria2.github.io/).

```bash
brew install aria2  # macOS
uv sync             # install Python dependencies
```

## Usage

### Search

```bash
# Interactive — shows picker, prompts for download
auto-torrent search "project hail mary"

# Auto-select best match and download immediately
auto-torrent search "the name of the wind" --auto

# JSON output for scripts/agents
auto-torrent search "the name of the wind" --json --limit 5

# Auto + JSON — search, download best match, return everything
auto-torrent search "the wise mans fear" --auto --json

# Prefer a specific narrator
auto-torrent search "the wise mans fear" --narrator Rupert Degas --json
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

1. **Open Library lookup** — resolves the query to canonical title, author, series, and cover ID
2. **ABB fan-out** — searches AudiobookBay with title, series, and author queries in parallel
3. **Detail enrichment** — scrapes narrator, format, bitrate from each result page (concurrent workers)
4. **Scoring** — fuzzy matches each result against canonical metadata (title 50%, author 30%, series 20%) with narrator preference bonus
5. **Download** — hands the magnet to aria2c with auto-stop on completion (`--seed-time=0`)

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
