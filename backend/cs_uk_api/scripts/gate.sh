#!/usr/bin/env bash
#
# scripts/gate.sh — live gate for a single provider
#
# Pipeline:
#   1. Activate project venv, launch uvicorn in the background on $PORT
#      (stdout/stderr captured at /tmp/gate-uvicorn-$$.log).
#   2. Poll $BASE/api/providers until it answers 2xx (max 30 x 0.3s = 9s).
#   3. GET /api/search?q=<query>&provider=<provider> — must yield >= 1 result.
#   4. GET /api/content/<id>  for results[0].id.
#   5. GET /api/stream/<id>   to obtain the playable URL.
#   6. Probe the URL with mpv (audio-only, 1 frame, error-level msg only)
#      under a 30s timeout.
#
# Why `set -euo pipefail`:
#   -e: exit on any unhandled failure (avoids running mpv on a bad URL).
#   -u: catch unset-variable typos (PROVIDER, QUERY, PORT).
#   -o pipefail: a failure in any pipeline segment (e.g. python3 parsing
#     an empty/garbage curl body) propagates, so a silently-empty
#     `$(curl ...)` assignment no longer masks the broken stage.
#
# Exit codes:
#   0  — GATE PASS (uvicorn ready, search/content/stream OK, mpv probed OK).
#   1  — GATE FAIL with a short reason on stderr.
#   2  — missing dependency at preflight.
#
# Usage:  gate.sh <provider> [query]
# Env:    PORT (default 8002)

set -euo pipefail

# --- preflight: required commands present? ---
for c in uvicorn python3 curl mpv timeout; do
  if ! command -v "$c" >/dev/null 2>&1; then
    echo "missing dep: $c" >&2
    exit 2
  fi
done

PROVIDER="${1:?usage: gate.sh <provider> [query]}"
QUERY="${2:-Дюна}"
PORT="${PORT:-8002}"

UVICORN_LOG="/tmp/gate-uvicorn-$$.log"
SERVER_PID=""

cleanup() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# --- start uvicorn (background, logs captured) ---
. "$(dirname "$0")/../../.venv/bin/activate"
uvicorn cs_uk_api.main:app --port "$PORT" --host 127.0.0.1 \
  >"$UVICORN_LOG" 2>&1 &
SERVER_PID=$!

BASE="http://127.0.0.1:${PORT}"

# --- readiness poll: wait until /api/providers responds 2xx ---
ready=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 "$BASE/api/providers" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.3
done
if [[ "$ready" -ne 1 ]]; then
  echo "GATE FAIL: uvicorn not ready on $BASE (log: $UVICORN_LOG)" >&2
  exit 1
fi

# --- search ---
if ! RESULTS=$(curl -fsS --max-time 30 --data-urlencode "q=$QUERY" --data-urlencode "provider=$PROVIDER" -G "$BASE/api/search"); then
  echo "GATE FAIL: /api/search request failed (log: $UVICORN_LOG)" >&2
  exit 1
fi
COUNT=$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])['results']))" "$RESULTS")
echo "search: $COUNT results"
if [ "$COUNT" = "0" ]; then echo "GATE FAIL: no results"; exit 1; fi

# --- content ---
CID=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['results'][0]['id'])" "$RESULTS")
if ! CONTENT=$(curl -fsS --max-time 30 "$BASE/api/content/$CID"); then
  echo "GATE FAIL: /api/content/$CID request failed (log: $UVICORN_LOG)" >&2
  exit 1
fi
echo "content: $(python3 -c "import json,sys; print(json.loads(sys.argv[1])['title'])" "$CONTENT")"

# --- stream ---
if ! STREAM=$(curl -fsS --max-time 30 "$BASE/api/stream/$CID"); then
  echo "GATE FAIL: /api/stream/$CID request failed (log: $UVICORN_LOG)" >&2
  exit 1
fi
URL=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['url'])" "$STREAM")
echo "stream: $URL"

# --- mpv probe ---
timeout 30 mpv --no-video --frames=1 --no-config --msg-level=all=error "$URL" \
  && echo "GATE PASS" || { echo "GATE FAIL: mpv"; exit 1; }
