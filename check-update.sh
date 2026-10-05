#!/usr/bin/env bash
# Compare the running ReSkate server image with the latest ReSkate GitHub release.
# Usage: check-update.sh [container]   (default reskate-server-1)
# Exit 0 = up to date, 10 = update available, 1 = check failed.
# Optional env: NOTIFY_WEBHOOK (URL, gets a text POST once per new release),
#               STATE_DIR (default ~/.cache/reskate-update-check)
set -u
container=${1:-reskate-server-1}
repo=Dingo-Shenanigans/ReSkate
hub=dudedankdave/reskate-server
state=${STATE_DIR:-$HOME/.cache/reskate-update-check}; mkdir -p "$state"
ts() { date -u +%FT%TZ; }

latest=$(curl -fsS -m 20 "https://api.github.com/repos/$repo/releases/latest" | python3 -c 'import sys,json;print(json.load(sys.stdin)["tag_name"].lstrip("v"))') \
  || { echo "$(ts) ERROR: could not read latest release of $repo"; exit 1; }
running=$(docker inspect "$container" --format '{{.Config.Image}}' >/dev/null 2>&1 && \
  docker image inspect "$(docker inspect "$container" --format '{{.Image}}')" --format '{{index .Config.Labels "org.opencontainers.image.version"}}') \
  || { echo "$(ts) ERROR: could not read image version of container $container"; exit 1; }

if [ "$running" = "$latest" ]; then
  echo "$(ts) OK: $container runs $running (latest release $latest)"; exit 0
fi

if curl -fsS -m 20 "https://hub.docker.com/v2/repositories/$hub/tags/$latest" >/dev/null 2>&1; then
  how="Docker Hub has $hub:$latest: run 'docker compose pull && docker compose up -d'."
else
  how="$hub:$latest is not on Docker Hub yet: build and push it first (see README, Building)."
fi
msg="ReSkate $latest is out, $container runs $running. $how Restarting kicks players and changes join codes."
echo "$(ts) UPDATE AVAILABLE: $msg"
if [ -n "${NOTIFY_WEBHOOK:-}" ] && [ "$(cat "$state/notified" 2>/dev/null)" != "$latest" ]; then
  curl -fsS -m 20 -X POST -H 'Content-Type: text/plain' --data "$msg" "$NOTIFY_WEBHOOK" >/dev/null && echo "$latest" > "$state/notified"
fi
exit 10
