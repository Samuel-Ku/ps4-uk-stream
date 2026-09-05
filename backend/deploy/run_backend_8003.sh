#!/usr/bin/env bash
# Run the real backend on :8003 — the Switchfin device port (see
# docs/test-artifacts/switchfin/device-driving.md) — with the torrent lane
# wired to the engine this directory's compose stack starts. Foreground.
#
# Prereqs: engine up (docker-compose.bitplay.yml). Export CS_UK_JF_TOKEN
# only if you need a specific facade token (a default is baked otherwise).
set -euo pipefail
cd "$(dirname "$0")/.."   # backend/

export CS_UK_TORRENT_ENGINE_URL="${CS_UK_TORRENT_ENGINE_URL:-http://192.168.2.166:3347}"
# Popcorn (#379 series lane) stays off by default — the #373 acceptance is
# a movie; export CS_UK_POPCORN_BASE_URL to add series.

exec ../.venv/bin/python -m uvicorn cs_uk_api.main:app --host 0.0.0.0 --port 8003
