#!/usr/bin/env bash
# Per-provider live gate (issue #30, spec §7.1).
#
# usage: gate.sh <provider> [query]   — gate one provider
#        gate.sh --all                — gate every registered provider
#
# Pipeline per provider (spec §7.1):
#   search → content → stream → mpv plays 1 frame (GATE PASS/FAIL)
#
# On mpv failure (grilling Q2 — diagnostic mode):
#   capture the player HTML, scan it for JS-generation markers
#   (eval(, Function(, atob(, obfuscated). A "not portable" verdict is
#   only issued on real marker evidence; otherwise the failure is
#   reported as upstream/network, not portability.
#
# On success (grilling Q4 — playability profile):
#   ffprobe the resolved URL → codec/resolution/bitrate; anything that
#   is not H.264 is flagged "ps4-soft-decode-risk" (mpv on PS4 decodes
#   in software).
set -uo pipefail

usage() {
    echo "usage: gate.sh <provider> [query] | gate.sh --all" >&2
    exit 2
}

[ "$#" -ge 1 ] || usage

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
. .venv/bin/activate

PORT="${PORT:-8002}"
BASE="http://127.0.0.1:${PORT}"
TIMEOUT_S="${GATE_TIMEOUT_S:-40}"
TMP="$(mktemp -d)"
trap 'kill "${SERVER_PID:-}" 2>/dev/null || true; rm -rf "$TMP"' EXIT

start_server() {
    uvicorn cs_uk_api.main:app --port "$PORT" --host 127.0.0.1 >"$TMP/uvicorn.log" 2>&1 &
    SERVER_PID=$!
    for _ in $(seq 1 30); do
        if curl -fsS "$BASE/api/providers" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.3
    done
    echo "GATE ERROR: uvicorn did not start on :$PORT (see $TMP/uvicorn.log)" >&2
    exit 1
}

# Headers arrive newline-joined (one "K: v" per line); render them into
# the formats each tool wants:
#   mpv    --http-header-fields: comma-separated list
#   ffprobe -headers:            newline-terminated string
#   curl   -H:                   repeated options
headers_mpv() {
    paste -sd, - <<< "$1"
}

# grilling Q4: ffprobe → codec/resolution/bitrate → risk flag
playability_profile() {
    local provider="$1" url="$2" headers_str="$3"
    local hdr_args=()
    [ -n "$headers_str" ] && hdr_args=(-headers "$headers_str")
    if ffprobe -v error -print_format json -show_streams -show_format \
        "${hdr_args[@]}" -rw_timeout 15000000 "$url" >"$TMP/ffprobe-$provider.json" 2>"$TMP/ffprobe-$provider.log"; then
        echo "PROFILE $provider: $(python3 -m cs_uk_api.gate_tools profile "$TMP/ffprobe-$provider.json")"
    else
        echo "PROFILE $provider: ffprobe failed (stream may be transient)"
    fi
}

# grilling Q2: on mpv failure, scan the player HTML for JS-generation
# markers. "not portable" only on real evidence.
diagnose() {
    local provider="$1" url="$2" headers_str="$3"
    local hdr_args=() line
    while IFS= read -r line; do
        [ -n "$line" ] && hdr_args+=(-H "$line")
    done <<< "$headers_str"
    if curl -fsSL --max-time 15 -A "Mozilla/5.0" "${hdr_args[@]}" "$url" -o "$TMP/player-$provider.html" 2>/dev/null; then
        markers=$(python3 -m cs_uk_api.gate_tools scan "$TMP/player-$provider.html")
        if [ "$markers" = "clean" ]; then
            echo "DIAG $provider: player HTML clean of JS markers — likely upstream/network issue (NOT not-portable)"
        else
            echo "DIAG $provider: JS-generation markers found: $markers — verdict: not portable (JS engine required)"
        fi
    else
        echo "DIAG $provider: could not capture player HTML (curl failed)"
    fi
}

gate_one() {
    local provider="$1" query="${2:-Дюна}"
    local results count cid content title stream url headers_str
    local tries=0 last_url="" last_headers=""

    # --- search ---
    results=$(curl -fsS "$BASE/api/search?q=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$query")&provider=$provider" 2>/dev/null) || {
        echo "GATE FAIL $provider: search (network or upstream)"
        return 1
    }
    count=$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])['results']))" "$results")
    if [ "$count" = "0" ]; then
        echo "GATE FAIL $provider: search returned 0 results"
        return 1
    fi

    # --- try up to 3 search results (top hit may be a trailer/no-player page) ---
    while [ "$tries" -lt 3 ] && [ "$tries" -lt "$count" ]; do
        cid=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['results'][$tries]['id'])" "$results")

        # --- content (failures advance the loop, like stream failures) ---
        content=$(curl -fsS "$BASE/api/content/$cid" 2>/dev/null) || {
            echo "GATE NOTE $provider: content ($cid) — trying next result"
            tries=$((tries + 1))
            continue
        }
        title=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['title'])" "$content")

        # --- stream ---
        stream=$(curl -fsS "$BASE/api/stream/$cid" 2>/dev/null) || {
            echo "GATE NOTE $provider: stream ($cid) — trying next result"
            tries=$((tries + 1))
            continue
        }
        url=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['url'])" "$stream")
        headers_str=$(python3 -c "
import json, sys
d = json.loads(sys.argv[1])
for k, v in d.get('headers', {}).items():
    print(f'{k}: {v}')
" "$stream")

        # --- mpv gate (--http-header-fields is comma-separated) ---
        MPV_EXTRA=()
        [ -n "$headers_str" ] && MPV_EXTRA=(--http-header-fields="$(headers_mpv "$headers_str")")
        if timeout "$TIMEOUT_S" mpv --no-config --no-video --frames=1 --msg-level=all=error \
            "${MPV_EXTRA[@]}" "$url" >"$TMP/mpv.log" 2>&1; then
            echo "GATE PASS $provider: $title"
            playability_profile "$provider" "$url" "$headers_str"
            return 0
        fi

        echo "GATE NOTE $provider: mpv did not play ($title) — trying next result"
        last_url="$url"
        last_headers="$headers_str"
        tries=$((tries + 1))
    done

    echo "GATE FAIL $provider: no playable stream across top $tries results"
    [ -n "$last_url" ] && diagnose "$provider" "$last_url" "$last_headers"
    return 1
}

start_server

if [ "${1:-}" = "--all" ]; then
    ids=$(curl -fsS "$BASE/api/providers" | python3 -c "import json,sys; print(' '.join(p['id'] for p in json.load(sys.stdin)))")
    [ -n "$ids" ] || { echo "GATE ERROR: no providers registered"; exit 1; }
    echo "Gating providers: $ids"
    rc=0
    for p in $ids; do
        gate_one "$p" "${2:-Дюна}" || rc=1
    done
    exit "$rc"
fi

gate_one "$1" "${2:-Дюна}"
