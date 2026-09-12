#!/usr/bin/env bash
# Deployment smoke test — the four flows a first real user drives, against a
# LIVE stack: the facade on :8003 and (for the play flow) the BitPlay engine.
#
#   1. handshake   POST /Users/AuthenticateByName  — accept-any login
#   2. views       GET  /UserViews                 — the libraries listing
#   3. browse      GET  /api/browse                — a live provider's page
#   4. play        search → detail → PlaybackInfo → stream bytes + seek
#
# One command for the deploy checklist (DEPLOY.md §6) and for post-update
# verification, instead of re-typing curl one-liners by hand. Exits 0 when
# every flow passed, 1 when any failed, 2 on usage error — so a wrapper
# (systemd, CI, an operator's `&&`) can trust the status.
#
# It mutates nothing shared: the play flow adds one torrent to the engine
# (idempotent by infohash; the engine reaps idle sessions) and no cache is
# purged — unlike accept_373.sh, this runs against a stack someone is using.
#
# Usage:
#   ./smoke.sh                 # all four flows
#   ./smoke.sh --skip-lane     # handshake + views + browse (host with no engine)
#
# Env:
#   CS_UK_API_URL       facade base URL          (default http://127.0.0.1:8003)
#   CS_UK_ENGINE_URL    engine base URL          (default http://127.0.0.1:3347)
#   CS_UK_JF_TOKEN      facade token             (default: the handshake's own)
#   CS_UK_TEST_TITLE    title for the play flow  (default "Dune")
#   CS_UK_TIMEOUT       per-request seconds      (default 60)
#   CS_UK_PLAY_TIMEOUT  PlaybackInfo seconds     (default 120; a cold swarm
#                       blocks on metadata, so this one gets headroom)
#   CS_UK_PLAY_ATTEMPTS candidates tried         (default 3)
#
# The play flow tries up to CS_UK_PLAY_ATTEMPTS search hits and passes on
# the first that yields a MediaSource. That is deliberate: a title can be
# listed by the lane and still carry no torrents (the #413/#415 class —
# yts.gg details payloads with an empty torrent map), which is an upstream
# gap, not a broken deployment. The gate fails only when NO candidate
# plays, which is what a dead lane or a misconfigured engine looks like.
#
# Only curl + python3 are required — both are already deployment prerequisites.
set -uo pipefail
# No `set -e` on purpose: a failing flow must be counted and reported, not
# abort the run, so the later flows still get to speak in the same report.

API="${CS_UK_API_URL:-http://127.0.0.1:8003}"
ENGINE="${CS_UK_ENGINE_URL:-http://127.0.0.1:3347}"
TOKEN="${CS_UK_JF_TOKEN:-}"
TITLE="${CS_UK_TEST_TITLE:-Dune}"
TIMEOUT="${CS_UK_TIMEOUT:-60}"
PLAY_TIMEOUT="${CS_UK_PLAY_TIMEOUT:-120}"
PLAY_ATTEMPTS="${CS_UK_PLAY_ATTEMPTS:-3}"
SKIP_LANE=0

case "${1:-}" in
  --skip-lane) SKIP_LANE=1 ;;
  "") ;;
  *) echo "usage: $0 [--skip-lane]" >&2; exit 2 ;;
esac

PASS=0; FAIL=0; SKIP=0
ok()   { echo "  PASS  $*"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }
skip() { echo "  SKIP  $*"; SKIP=$((SKIP+1)); }
step() { echo; echo "== $* =="; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# The two header channels the facade accepts (auth.py) are equivalent; this
# script uses the canonical one. AUTH stays empty until a token exists.
AUTH=()
TOKEN_READY=0
set_token() {
  TOKEN="$1"
  AUTH=(-H "X-Emby-Token: $TOKEN")
  TOKEN_READY=1
}

json_get() { # file 'expr' -> value (empty on any parse failure)
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print($2)" "$1" 2>/dev/null
}

flow_handshake() {
  step "1) handshake — accept-any login (POST /Users/AuthenticateByName)"
  local code
  code=$(curl -s -m "$TIMEOUT" -X POST -H 'Content-Type: application/json' \
    -d '{"Username":"smoke","Pw":"smoke"}' -o "$TMP/auth.json" -w '%{http_code}' \
    "$API/Users/AuthenticateByName")
  if [ "$code" != "200" ]; then
    bad "POST /Users/AuthenticateByName -> $code"
    return 1
  fi
  local issued
  issued=$(json_get "$TMP/auth.json" 'd.get("AccessToken","")')
  [ -n "$issued" ] || { bad "200 but no AccessToken in the body"; return 1; }
  ok "200, token issued (ServerId=$(json_get "$TMP/auth.json" 'd.get("ServerId","")'))"
  if [ -z "$TOKEN" ]; then
    set_token "$issued"
    ok "using the handshake token for the authed flows"
  else
    ok "CS_UK_JF_TOKEN provided; authed flows will use it"
  fi
  return 0
}

flow_views() {
  step "2) views — GET /UserViews (the spelling Switchfin's SDK sends)"
  local code
  code=$(curl -s -m "$TIMEOUT" "${AUTH[@]}" -o "$TMP/views.json" -w '%{http_code}' \
    "$API/UserViews")
  [ "$code" = "200" ] || { bad "GET /UserViews -> $code"; return 1; }
  local n
  n=$(json_get "$TMP/views.json" 'len(d.get("Items",[]))')
  if [ "${n:-0}" -gt 0 ]; then
    ok "200, $n views"
    python3 -c "
import json
for v in json.load(open('$TMP/views.json'))['Items'][:5]:
    print('        -', v.get('Name'))
" 2>/dev/null
    return 0
  fi
  bad "200 but the views listing is empty"
  return 1
}

flow_browse() {
  step "3) browse — a live provider/section answers a page"
  local code
  code=$(curl -s -m "$TIMEOUT" "${AUTH[@]}" -o "$TMP/sections.json" -w '%{http_code}' \
    "$API/api/sections")
  [ "$code" = "200" ] || { bad "GET /api/sections -> $code"; return 1; }

  # Walk the advertised sections until one answers a non-empty page: the
  # flow is "browse works", not "this particular site is reachable", and a
  # provider can be upstream-degraded without the deployment being broken.
  local pid sid saw_empty=0
  while read -r pid sid; do
    [ -n "$pid" ] || continue
    code=$(curl -s -m "$TIMEOUT" "${AUTH[@]}" -o "$TMP/browse.json" -w '%{http_code}' \
      "$API/api/browse?provider=$pid&section=$sid")
    if [ "$code" = "200" ]; then
      local n
      n=$(json_get "$TMP/browse.json" 'len(d.get("results",[]))')
      if [ "${n:-0}" -gt 0 ]; then
        ok "GET /api/browse?provider=$pid&section=$sid -> 200, $n cards"
        return 0
      fi
      saw_empty=1
    fi
  done < <(python3 -c "
import json
for p in json.load(open('$TMP/sections.json')):
    for s in p.get('sections') or []:
        print(p['provider'], s['id'])
" 2>/dev/null)

  if [ "$saw_empty" = "1" ]; then
    bad "every browsable section returned 200 with an empty page"
  else
    bad "no advertised provider/section answered a page"
  fi
  return 1
}

PLAY_PATH=""    # set by candidate_play on success
PLAY_REASON=""  # why the last candidate was skipped

candidate_play() { # group key -> 0 when it produced a MediaSource
  local gk="$1" code name
  PLAY_PATH=""; PLAY_REASON=""

  # The client opens the item before playing; this also registers the group
  # in the resolution map the play flow reads (a key that was never resolved
  # 404s — the #415 class).
  code=$(curl -s -m "$TIMEOUT" "${AUTH[@]}" -o "$TMP/detail.json" -w '%{http_code}' \
    "$API/Items/$gk")
  if [ "$code" != "200" ]; then
    PLAY_REASON="detail -> $code $(json_get "$TMP/detail.json" 'd.get("detail") or ""')"
    return 1
  fi
  name=$(json_get "$TMP/detail.json" 'd.get("Name","?")')
  echo "        detail 200 ($name)"

  code=$(curl -s -m "$PLAY_TIMEOUT" "${AUTH[@]}" -H 'Content-Type: application/json' \
    -X POST -d '{}' -o "$TMP/pi.json" -w '%{http_code}' "$API/Items/$gk/PlaybackInfo")
  if [ "$code" != "200" ]; then
    PLAY_REASON="PlaybackInfo -> $code $(json_get "$TMP/pi.json" 'd.get("detail") or ""')"
    return 1
  fi
  PLAY_PATH=$(json_get "$TMP/pi.json" '(d.get("MediaSources") or [{}])[0].get("Path","")')
  if [ -z "$PLAY_PATH" ]; then
    PLAY_REASON="PlaybackInfo 200 with no MediaSources[].Path"
    return 1
  fi
  return 0
}

flow_play() {
  step "4) torrent-lane play — search → detail → PlaybackInfo → bytes + seek"
  if [ "$SKIP_LANE" = "1" ]; then
    skip "lane flow skipped (--skip-lane)"
    return 0
  fi

  local q code gk
  q=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$TITLE")
  code=$(curl -s -m "$TIMEOUT" "${AUTH[@]}" -o "$TMP/search.json" -w '%{http_code}' \
    "$API/api/search?q=$q&provider=yts")
  [ "$code" = "200" ] || { bad "GET /api/search?q=$TITLE&provider=yts -> $code"; return 1; }

  local CANDS=()
  mapfile -t CANDS < <(python3 -c "
import json, sys
n = int(sys.argv[1])
g3 = [g['group_key'] for g in json.load(open(sys.argv[2])).get('groups', [])
      if g['group_key'].startswith('g3:')]
print('\n'.join(g3[:n]))
" "$PLAY_ATTEMPTS" "$TMP/search.json" 2>/dev/null)
  if [ "${#CANDS[@]}" -eq 0 ]; then
    bad "search for '$TITLE' returned no torrent-lane group"
    return 1
  fi
  ok "search '$TITLE' -> ${#CANDS[@]} candidate(s): ${CANDS[*]}"

  for gk in "${CANDS[@]}"; do
    # Re-issue the search before each attempt. A group known to the catalog
    # but dropped from the resolution map 404s at detail — the #415 class,
    # observed live when a background index rebuild lands mid-run. The
    # search itself is served from cache, so this costs ~0s and makes the
    # gate's verdict independent of that race.
    curl -s -m "$TIMEOUT" "${AUTH[@]}" -o /dev/null "$API/api/search?q=$q&provider=yts"
    if candidate_play "$gk"; then
      ok "playable group $gk -> $PLAY_PATH"
      break
    fi
    skip "$gk: $PLAY_REASON"
  done
  [ -n "$PLAY_PATH" ] || { bad "no candidate played '$TITLE' — lane or engine is down"; return 1; }

  # The facade never proxies the media: it hands the player the engine URL.
  local loc
  loc=$(curl -s -m "$TIMEOUT" "${AUTH[@]}" -o /dev/null -w '%{redirect_url}' "$API$PLAY_PATH")
  case "$loc" in
    "$ENGINE"/*) ok "stream -> 302 to the engine ($loc)" ;;
    "")          bad "stream returned no redirect (expected $ENGINE/...)"; return 1 ;;
    *)           bad "stream redirected somewhere unexpected: $loc"; return 1 ;;
  esac

  # Bytes must actually flow — a 302 to a dead swarm is not a working lane.
  local first want=65536
  first=$(curl -sL -m "$TIMEOUT" -r 0-$((want-1)) -o /dev/null -w '%{http_code} %{size_download}' "$loc")
  case "$first" in
    "206 $want"|"200 $want") ok "first 64 KiB through the redirect chain ($first)" ;;
    *) bad "bounded read failed ($first; want 206/200 with $want bytes)"; return 1 ;;
  esac

  local seek
  seek=$(curl -sL -m "$TIMEOUT" -r 1048576-2097151 -D "$TMP/seek.h" -o /dev/null \
    -w '%{http_code} %{size_download}' "$loc")
  if [ "$seek" = "206 1048576" ] && grep -qi '^content-range: bytes 1048576-' "$TMP/seek.h"; then
    ok "seek works ($seek, Content-Range honoured)"
  else
    bad "seek failed ($seek; want 206 with 1048576 bytes)"
    return 1
  fi

  return 0
}

echo "smoke: $API (engine $ENGINE)"
# A token supplied up front lets the authed flows run even if the handshake
# endpoint itself is the thing that broke.
[ -n "$TOKEN" ] && set_token "$TOKEN"

if flow_handshake || [ "$TOKEN_READY" = "1" ]; then
  flow_views
  flow_browse
  flow_play
else
  echo
  echo "handshake failed and no CS_UK_JF_TOKEN was supplied — skipping the authed flows"
  SKIP=$((SKIP+3))
fi

echo
echo "=== $PASS passed, $FAIL failed, $SKIP skipped ==="
[ "$FAIL" -eq 0 ] || exit 1
exit 0
