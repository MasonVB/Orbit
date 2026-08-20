#!/usr/bin/env bash
# Run Orbit straight from source in a venv - no Docker, fastest to iterate.
#
#   ./scripts/dev.sh          web + worker on http://localhost:8899
#   ./scripts/dev.sh test     smoke test only
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  echo "==> creating .venv"
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt httpx
fi
source .venv/bin/activate

for tool in ffmpeg exiftool; do
  command -v "$tool" >/dev/null || echo "warning: $tool not found (sudo apt install ffmpeg libimage-exiftool-perl)"
done

# The smoke test manages its own scratch library and credentials, so run it
# before anything is exported into the environment.
if [[ "${1:-}" == "test" ]]; then
  shift
  exec python smoke_test.py "$@"
fi

export ORBIT_DATA="${ORBIT_DATA:-$PWD/data}"
export ORBIT_ADMIN_PASSWORD="${ORBIT_ADMIN_PASSWORD:-localtest}"
export ORBIT_PUBLIC_URL="${ORBIT_PUBLIC_URL:-http://localhost:8899}"
mkdir -p "$ORBIT_DATA"

echo "==> data in $ORBIT_DATA"
python -m app.worker &
WORKER=$!
trap 'kill $WORKER 2>/dev/null || true' EXIT
exec uvicorn app.main:app --host 0.0.0.0 --port 8899 --reload
