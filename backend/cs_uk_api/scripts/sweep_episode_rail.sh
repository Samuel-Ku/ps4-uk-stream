#!/usr/bin/env bash
# Episode-rail verification sweep (issue #136) — re-runnable against a
# live server. Boots the API on 127.0.0.1:${PORT:-8002}, warms the home
# snapshot, then walks the series episode-rail path for every provider:
#
#   /Shows/{g1}/Seasons
#     -> /Shows/{g1}/Episodes?seasonId={season}
#     -> POST /Items/{ep}/PlaybackInfo
#     -> GET  /Videos/{ep}/stream?static=true
#
# Per-provider results (✅ / 🐛 / ⚠️ / ⏭️) print to stdout and, when the
# facade resolves a live home, write to docs/sweep-episode-rail-<date>.md.
#
# Diagnostic only: no adapter/facade changes. Mirror of gate.sh's
# boot-then-run shape. Requires uvicorn + python3 + curl on PATH.
set -euo pipefail

PORT="${PORT:-8002}"
PER_PROVIDER="${PER_PROVIDER:-3}"
TOKEN="${CS_UK_JF_TOKEN:-jellyfin-dev-token}"
OUT_DIR="$(cd "$(dirname "$0")/../../.." && pwd)/docs"
DATE="$(date +%F)"
REPORT="${OUT_DIR}/sweep-episode-rail-${DATE}.md"

# Preflight: fail fast with a clear message if a dependency is missing.
for dep in uvicorn python3 curl; do
  command -v "$dep" >/dev/null 2>&1 || { echo "GATE ERROR: missing dependency: $dep" >&2; exit 2; }
done

cd "$(dirname "$0")/../.."
. .venv/bin/activate

SERVER_PID=""
cleanup() { [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null || true; }
trap cleanup EXIT

echo "booting cs-uk-api on 127.0.0.1:${PORT} ..."
uvicorn cs_uk_api.main:app --port "$PORT" --host 127.0.0.1 >/tmp/sweep_episode_rail_server.log 2>&1 &
SERVER_PID=$!

# Wait for the server to accept connections (up to ~15s), then assert it
# actually came up before running the sweep (gate.sh does the same).
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORT}/System/Info/Public" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if ! curl -fsS "http://127.0.0.1:${PORT}/System/Info/Public" >/dev/null 2>&1; then
  echo "GATE ERROR: server did not come up on 127.0.0.1:${PORT}" >&2
  echo "--- server log ---" >&2
  tail -n 20 /tmp/sweep_episode_rail_server.log >&2 || true
  exit 1
fi

echo "running episode-rail sweep (per_provider=${PER_PROVIDER}) ..."
python -m cs_uk_api.sweep_episode_rail http "http://127.0.0.1:${PORT}" \
  --token "$TOKEN" --per-provider "$PER_PROVIDER" --out "$REPORT"

echo "report written to ${REPORT}"
