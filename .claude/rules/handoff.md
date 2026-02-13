# Last Session Handoff

## Done
- **TPB code quality improvements shipped**: (1) `parse_title()` returns `TitleInfo` NamedTuple, (2) guarded malformed API responses with try/except, (3) moved scoring constants to config.py (`TPB_SOURCE_SCORES`, etc), (4) stream.py MIME type via `mimetypes.guess_type()`, (5) `_resolve_status` checks download directory for files (not just PID), (6) added 6 new tests (malformed item, empty API list, TitleInfo isinstance, TPBResult serialization, resolve status failed/completed)
- Test suite: 106 tests passing (100 existing + 6 new)
- Explored Stremio + Torrentio/PPVStreams as alternative GUI for movies/TV/sports — better than auto-torrent UX for that use case

## Decisions
- auto-torrent focuses on audiobooks (ABB) where it adds value. Movies/TV/sports better served by Stremio addon ecosystem
- Audiobook integration with Basil agent: JSON CLI output pattern from auto-torrent ✓
- Architecture exploration started for Basil + basil-mobile + auto-torrent integration

## Next
- Design architecture for Basil agent + auto-torrent integration (scope: audiobooks search/download/streaming)
- Determine storage strategy: local server vs Google Drive for downloads, streaming origin
- Plan basil-mobile audiobook playback features
