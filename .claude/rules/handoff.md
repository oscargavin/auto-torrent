# Last Session Handoff

## Done
- Refactored CLI entry points: `_build_parser(prog, default_source)` → `main_atb()` (AudiobookBay default), `main_atv()` (TPB default), backward-compatible `main()` generic. Updated pyproject.toml with 3 scripts
- Global installation via `uv tool install` — removes `uv run` prefix, commands available directly as `atb`, `atv`, `auto-torrent`
- Updated README with new usage examples (atb/atv)
- Clarified help text: `stream` takes magnet URIs, use `search --stream` for combined flow (fixes "unsupported URL protocol" UX confusion)

## Next
- Implement libtorrent downloads: replace aria2c in `download()`, reuse probe session for peer continuity
- Add LLM result disambiguation: `_llm_select()` picks best match from equally-scored results
- Test multi-title queries ("red rising 1 vs 6") end-to-end
