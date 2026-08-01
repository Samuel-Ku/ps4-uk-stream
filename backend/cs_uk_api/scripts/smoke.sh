#!/usr/bin/env bash
set -euo pipefail
PORT="${PORT:-8001}"
cd "$(dirname "$0")/../.."
. .venv/bin/activate
uvicorn cs_uk_api.main:app --port "$PORT" --host 127.0.0.1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID"' EXIT
sleep 1
curl -sS "http://127.0.0.1:${PORT}/api/providers" | python -m json.tool
curl -sS "http://127.0.0.1:${PORT}/api/search?q=Дюна" | python -m json.tool
echo "smoke OK"
