# Last Session Handoff

## Done
- config.py: `LLM_TIMEOUT` 30→90s (claude -p cold start needs headroom)
- abb.py: request timeout 15→45s (proxy ~29s per search query)
- Pushed both changes; deployed via webhook
- Fixed venv: `uv sync --extra server` (was missing anyio for server extras)
- Service healthy, SMS server running

## Decisions
- 90s LLM timeout: `claude -p --json-schema` subprocess can take 10-30s on first run + retries
- 45s ABB timeout: proxy residential IPs slower but required (direct server IP blocked by ABB on search)
- DataImpulse country targeting `__cr.gb` in config improves pool quality

## Next
- Test SMS: search "Iron Gold part 1 of 2" — should now return correct result in ~2-3 min
- Monitor if proxy IPs degrade again (check DataImpulse pool health dashboard)
