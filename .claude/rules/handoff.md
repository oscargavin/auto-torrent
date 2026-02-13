# Last Session Handoff

## Done
- Added `--json` flag: search-only mode returns structured results for agent inspection; with `--auto` also downloads and includes path
- 23 tests passing, CLI verified working
- Identified integration path: JSON CLI tool (not MCP server) fits Basil's existing architecture best

## Decisions
- JSON output over MCP — simpler integration, Basil calls `uv run auto-torrent --json`, no new process to manage
- Subcommands architecture — enables conversational flow (search → picker → download specific result). Planned: `auto-torrent search`, `auto-torrent download <magnet>`, `auto-torrent status <id>`

## Next
- Implement subcommands: search/download/status split
- Add rate-limit error messages (agent needs to know *why* it failed, not just 0 results)
- Add `--limit N` for result filtering
- Add `--background` flag + status polling for non-blocking downloads
