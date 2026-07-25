#!/usr/bin/env bash
# Keep the Claude Code credential alive, and shout early when it isn't.
#
# Claude Code refreshes its OAuth access token only when the CLI actually runs.
# This box goes days between book requests, so nothing triggered a refresh and
# the refresh token itself eventually lapsed — the agent was dead from
# 2026-06-20 to 2026-07-25 and nobody could tell, because a broken agent used
# to present as a spinner that never resolved.
#
# Running a trivial prompt on a timer keeps the refresh cycle turning, and
# turns "silently broken for five weeks" into "logged the same day".
set -uo pipefail

STATE_DIR="${HOME}/.local/state/atb"
STATE_FILE="${STATE_DIR}/claude-auth.json"
CREDS="${HOME}/.claude/.credentials.json"
WARN_DAYS="${WARN_DAYS:-3}"

mkdir -p "$STATE_DIR"

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

write_state() {
  # $1 = ok|failed, $2 = detail
  printf '{"checked_at":"%s","status":"%s","detail":"%s","expires_at":"%s"}\n' \
    "$(now)" "$1" "$2" "${EXPIRES_ISO:-unknown}" > "$STATE_FILE"
}

# --- how long is the current access token good for? ---
EXPIRES_ISO=unknown
DAYS_LEFT=unknown
if [ -r "$CREDS" ]; then
  read -r EXPIRES_ISO DAYS_LEFT < <(python3 - "$CREDS" <<'PY'
import json, sys, datetime
try:
    o = json.load(open(sys.argv[1])).get("claudeAiOauth", {})
    e = o.get("expiresAt")
    if e:
        exp = datetime.datetime.fromtimestamp(e / 1000, datetime.timezone.utc)
        days = (e / 1000 - datetime.datetime.now(datetime.timezone.utc).timestamp()) / 86400
        print(exp.strftime("%Y-%m-%dT%H:%M:%SZ"), f"{days:.2f}")
    else:
        print("unknown", "unknown")
except Exception:
    print("unknown", "unknown")
PY
  )
fi

# --- the actual keepalive: a real inference call, which is what refreshes ---
if OUT=$(timeout 120 claude -p "Reply with the single word: ok" </dev/null 2>&1); then
  if printf '%s' "$OUT" | grep -qi "ok"; then
    echo "claude-keepalive: OK (token expires ${EXPIRES_ISO}, ${DAYS_LEFT} days)"
    write_state ok "credential valid"
    exit 0
  fi
  # Exit 0 but no usable answer — treat as a failure, not a pass. A silent
  # empty response is exactly how the expired credential presented.
  echo "claude-keepalive: FAILED — CLI returned no usable output" >&2
  write_state failed "empty response"
  exit 1
fi

echo "claude-keepalive: FAILED — claude CLI errored" >&2
echo "claude-keepalive: output: ${OUT:0:400}" >&2
echo "claude-keepalive: FIX — ssh into this host (an interactive shell, not" >&2
echo "                  a one-shot ssh command: the login TUI needs a TTY)," >&2
echo "                  run 'claude' and log in. Then restart atb-arq-worker." >&2
write_state failed "cli error"
exit 1
