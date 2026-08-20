#!/usr/bin/env bash
# Pull the latest source and restart Orbit locally (Linux Mint / any desktop).
#
#   ./scripts/update.sh              rebuild from local source
#   ./scripts/update.sh --pull       run the published GHCR image instead
#
# Your library in ./data is never touched.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.local.yaml"
docker compose version >/dev/null 2>&1 || COMPOSE="docker-compose -f docker-compose.local.yaml"

echo "==> fetching"
git pull --ff-only

if [[ "${1:-}" == "--pull" ]]; then
  echo "==> pulling published image"
  $COMPOSE pull
else
  echo "==> building"
  $COMPOSE build
fi

echo "==> restarting"
UID_GID="$(id -u):$(id -g)" $COMPOSE up -d

echo "==> waiting for health"
for _ in $(seq 1 30); do
  if curl -fsS http://localhost:8899/healthz >/dev/null 2>&1; then
    echo "orbit is up: http://localhost:8899"
    exit 0
  fi
  sleep 2
done

echo "did not come up; recent logs:" >&2
$COMPOSE logs --tail=40 >&2
exit 1
