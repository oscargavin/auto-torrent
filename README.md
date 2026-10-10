# auto-torrent

CLI and chat server for finding and downloading audiobooks and streaming video via BitTorrent. Uses LLMs to parse freeform queries, disambiguate results, and handle conversational requests from the Bookkeeper app.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
# Install as global CLI tools (atb, atv, auto-torrent)
uv tool install --editable .

# Or run from source
uv sync
uv run atb search "project hail mary"
```

## CLI

Three entry points with different defaults:

| Command | Default source | Use case |
|---------|---------------|----------|
| `atb` | AudiobookBay | Audiobooks |
| `atv` | The Pirate Bay | Movies / TV |
| `auto-torrent` | None (specify `--source`) | Generic |

### Search

```bash
# Interactive picker
atb search "project hail mary"

# Auto-select best match and download
atb search "the name of the wind" --auto

# Prefer a specific narrator
atb search "the wise mans fear" --narrator Rupert Degas

# JSON output for scripts/agents
atb search "red rising" --json --limit 5

# Movies / TV
atv search "interstellar 2014" --quality 1080p --min-seeds 10
```

### Download

```bash
# Foreground (blocks until done)
atb download "magnet:?xt=urn:btih:..." --title "Project Hail Mary"

# Background (returns immediately with a download ID)
atb download "magnet:?xt=urn:btih:..." --title "Project Hail Mary" --bg --json
```

### Stream

```bash
# Search and stream in one step
atv search "interstellar" --stream --player iina

# Stream a known magnet
atv stream "magnet:?xt=urn:btih:..." --player mpv
```

Supports mpv, iina, and vlc. Auto-detects whichever is installed.

### Status

```bash
atb status              # list all downloads
atb status a1b2c3d4     # check a specific download
```

## Chat server

A FastAPI server that turns chat messages from the Bookkeeper app into audiobook downloads. Send a book title (even misspelled) and it searches, downloads, organises the files into Audiobookshelf, and reports progress on the chat card.

### How it works

1. **`POST /chat/threads/{id}/messages`** stores the message and queues a turn on the arq worker
2. **The agent** reads the conversation, searches, and either asks a question or commits to a book
3. **The download poll** watches the torrent and falls back to another copy on a stall
4. **File organiser** moves files into `Author/Title/` structure for ABS
5. **ABS library scan** triggers so the book appears in the app
6. **Server-sent events** on `/chat/threads/{id}/events` and `/chat/jobs/{id}/events` report each step

`GET /chat/jobs*` reads and lists the download jobs, and `DELETE /chat/jobs/{id}` cancels one.

### Conversation features

- Corrects misspellings: "hairy potter" -> searches "Harry Potter and the Philosopher's Stone"
- Resolves vague descriptions: "man stuck on mars" -> "The Martian"
- Suggests similar books: "something like Project Hail Mary" -> options to choose from
- Handles follow-ups in the same thread

### Server setup

```bash
# Install with server dependencies
uv sync --extra server

# Configure (copy and edit)
cp .env.example .env

# Run
uv run atb-server
```

Requires: Redis, Audiobookshelf instance, Claude CLI (`claude` in PATH). Run the worker with `uv run arq auto_torrent.server.jobs.worker.WorkerSettings`.

## How it works

### AudiobookBay search pipeline

1. **LLM query parsing** — Sonnet extracts title, author, narrator, series from freeform text
2. **Open Library lookup** — resolves canonical metadata and cover art
3. **ABB fan-out** — parallel searches by title, series, and author
4. **Detail enrichment** — concurrent scraping of narrator, format, bitrate from result pages
5. **Fuzzy scoring** — 50% title + 30% author + 20% series, narrator bonus/penalty
6. **LLM disambiguation** — Sonnet ranks tied results (e.g. book 1 vs book 6 in a series)
7. **DHT peer probing** — libtorrent probes top 3 picks for live seeders
8. **Download** — libtorrent with resume support, progress tracking, suspicious file scanning

### The Pirate Bay search pipeline

1. **API query** — apibay.org JSON API (no scraping)
2. **Title parsing** — regex extracts resolution, source, codec, HDR from torrent names
3. **Weighted scoring** — seeders (log-scale), source quality, resolution preference, uploader trust
4. **LLM ranking** — Sonnet picks best results considering content match and quality
5. **Peer probing + download** — same libtorrent pipeline as audiobooks

### Streaming

Starts a local HTTP Range server, prioritizes sequential pieces for playback, and launches the media player. MP4 moov atoms are pre-prioritized for fast start.

## Agent integration

All subcommands support `--json` for structured output. Typical agent flow:

```bash
atb search "project hail mary" --json --limit 3    # get results
atb download "magnet:?xt=..." --title "..." --bg --json  # start download
atb status a1b2c3d4 --json                          # poll until done
```

Errors return `{"error": "message"}`. Used by [Basil](https://github.com/oscargavin/basil) as MCP tools.

## License

MIT

## Deploying

A push to `main` deploys to the server. GitHub calls `/webhook/github`, which
starts `deploy/deploy.sh` as its own systemd unit, `atb-deploy`. The script
fast-forwards the checkout, syncs dependencies, restarts `atb-arq-worker` and
`atb-server`, and waits for `/health`. To see the last deploy:

```bash
ssh basil-server journalctl --user -u atb-deploy -n 20
```
