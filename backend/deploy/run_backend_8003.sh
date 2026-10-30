#!/usr/bin/env bash
# Run the real backend on :8003 — the Switchfin device port (see
# docs/test-artifacts/switchfin/device-driving.md) — with the torrent lane
# wired to the engine this directory's compose stack starts.
#
# Foreground supervisor: starts uvicorn, waits for /api/health, then waits
# for the process. Ctrl+C/SIGTERM stops the child too.
#
# Prereqs: engine up (docker-compose.bitplay.yml). Export CS_UK_JF_TOKEN
# only if you need a specific facade token (a default is baked otherwise).
# Override CS_UK_HOST (default 0.0.0.0) or CS_UK_PORT (default 8003) only
# when the acceptance stack needs a different bind.
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BACKEND_DIR"

PYTHON="$BACKEND_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "error: backend venv not found at $PYTHON" >&2
  echo "create it with: python3 -m venv '$BACKEND_DIR/.venv'" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "error: curl is required for the startup health check" >&2
  exit 1
fi

HOST="${CS_UK_HOST:-0.0.0.0}"
PORT="${CS_UK_PORT:-8003}"

# Fail before spawning uvicorn if the port is already taken: a second
# process would only die with "address already in use" after startup.
if command -v ss >/dev/null 2>&1; then
  listeners="$(ss -H -ltn "sport = :$PORT" 2>/dev/null || true)"
  if [[ -n "$listeners" ]]; then
    echo "error: port $PORT is already in use; stop the existing API first" >&2
    exit 1
  fi
fi

export CS_UK_TORRENT_ENGINE_URL="${CS_UK_TORRENT_ENGINE_URL:-http://192.168.2.166:3347}"
# Popcorn (#379 series lane). Live mirrors churn; probed 2026-10-28:
# fusme.link / jfper.link / uxert.link / yrkde.link.
export CS_UK_POPCORN_BASE_URL="${CS_UK_POPCORN_BASE_URL:-https://fusme.link}"

"$PYTHON" -m uvicorn cs_uk_api.main:app --host "$HOST" --port "$PORT" &
pid=$!

cleanup() {
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.25
    done
    kill -KILL "$pid" 2>/dev/null || true
  fi
  wait "$pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

HEALTH_HOST="$HOST"
if [[ "$HOST" == "0.0.0.0" ]]; then
  HEALTH_HOST="127.0.0.1"
fi
health_url="http://${HEALTH_HOST}:${PORT}/api/health"
for _ in $(seq 1 60); do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "error: uvicorn exited before the health check passed" >&2
    exit 1
  fi
  code="$(curl -sS -m 2 -o /dev/null -w '%{http_code}' "$health_url" 2>/dev/null || true)"
  if [[ "$code" == "200" ]]; then
    echo "cs-uk-api ready on ${HOST}:${PORT} (health 200)" >&2
    break
  fi
  sleep 1
done

if [[ "${code:-}" != "200" ]]; then
  echo "error: cs-uk-api health check failed at $health_url (last HTTP ${code:-none})" >&2
  exit 1
fi

wait "$pid"
