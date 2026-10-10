#!/bin/bash
# Deploy origin/main on basil. The GitHub webhook in server/app.py starts this
# as its own systemd unit (atb-deploy), never as a child of atb-server:
# restarting atb-server used to kill this script half-way, so the worker kept
# running the old code. The fixed unit name also stops two deploys running at
# once, which is how `git fetch` once failed with "cannot lock ref".
set -euo pipefail
cd /home/oscar/auto-torrent

# Loop until nothing new arrives, so a push that lands while a deploy runs is
# deployed too (its webhook could not start a second unit).
while true; do
  git fetch --quiet origin main
  if [ "$(git rev-parse HEAD)" = "$(git rev-parse FETCH_HEAD)" ]; then
    break
  fi
  git merge --ff-only --quiet FETCH_HEAD
  ~/.local/bin/uv sync --quiet --extra server
  systemctl --user restart atb-arq-worker atb-server
  echo "deployed $(git log --oneline -1)"
done

# A deploy is done when the server answers, not when the restart returns.
for _ in $(seq 1 30); do
  if curl -sf http://localhost:8004/health > /dev/null; then
    echo "healthy at $(git log --oneline -1)"
    exit 0
  fi
  sleep 1
done
echo "atb-server is not healthy after the deploy" >&2
exit 1
