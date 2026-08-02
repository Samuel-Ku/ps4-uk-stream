#!/usr/bin/env bash
set -euo pipefail
PROVIDER="${1:?usage: gate.sh <provider> [query]}"
QUERY="${2:-Дюна}"
PORT="${PORT:-8002}"
. "$(dirname "$0")/../../.venv/bin/activate"
uvicorn cs_uk_api.main:app --port "$PORT" --host 127.0.0.1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT
sleep 1
BASE="http://127.0.0.1:${PORT}"
RESULTS=$(curl -sS "$BASE/api/search?q=$QUERY&provider=$PROVIDER")
COUNT=$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])['results']))" "$RESULTS")
echo "search: $COUNT results"
if [ "$COUNT" = "0" ]; then echo "GATE FAIL: no results"; exit 1; fi
CID=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['results'][0]['id'])" "$RESULTS")
CONTENT=$(curl -sS "$BASE/api/content/$CID")
echo "content: $(python3 -c "import json,sys; print(json.loads(sys.argv[1])['title'])" "$CONTENT")"
STREAM=$(curl -sS "$BASE/api/stream/$CID")
URL=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['url'])" "$STREAM")
echo "stream: $URL"
timeout 30 mpv --no-video --frames=1 --no-config --msg-level=all=error "$URL" \
  && echo "GATE PASS" || { echo "GATE FAIL: mpv"; exit 1; }
