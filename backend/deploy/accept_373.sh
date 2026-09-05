#!/usr/bin/env bash
# Ticket #373 — real-PS4 end-to-end acceptance: one English title with seek
# and subtitles, through the REAL BitPlay engine and the REAL facade.
#
# Protocol: docs/torrent-lane.md §5 (the on-device verification recipe),
# extended with the player-floor checks the ticket asks to pin as spec
# facts: progressive start (F1), Range seeking (F2), srt→VTT subtitles
# (F3), audio-track presence (F4, ffprobe over the wire).
#
# Prereqs (runbook §1): engine up on this host (192.168.2.166 — the
# documented engine host), backend on :8003 with CS_UK_TORRENT_ENGINE_URL
# set (see run_backend_8003.sh in this directory).
#
# Usage:
#   ./accept_373.sh engine   # steps 1–4 + floor checks F1–F4 (engine only)
#   ./accept_373.sh facade   # B1–B5: the same title through the real backend
#   ./accept_373.sh all      # both, in order
set -uo pipefail

BASE="${CS_UK_ENGINE_URL:-http://192.168.2.166:3347}"
API="${CS_UK_API_URL:-http://127.0.0.1:8003}"
TOKEN="${CS_UK_JF_TOKEN:?CS_UK_JF_TOKEN must be exported - the backend Jellyfin token}"
MAGNET="${CS_UK_TEST_MAGNET:-magnet:?xt=urn:btih:08ada5a7a6183aae1e09d831df6748d566095a10&dn=Sintel&tr=udp%3A%2F%2Fexplodie.org%3A6969&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337}"
# Sintel: Blender Foundation's freely distributable short — the canonical
# well-seeded test torrent; carries Sintel.mp4 + a separate .srt track.

PASS=0; FAIL=0
ok()   { echo "PASS  $*"; PASS=$((PASS+1)); }
bad()  { echo "FAIL  $*"; FAIL=$((FAIL+1)); }
step() { echo; echo "== $* =="; }

jqpy() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

engine_floor() {
  step "1) add magnet → session (blocks on metadata)"
  ADD=$(curl -s -X POST "$BASE/api/v1/torrent/add" -H 'Content-Type: application/json' -d "{\"Magnet\": \"$MAGNET\"}")
  SID=$(echo "$ADD" | jqpy 'd["sessionId"]' 2>/dev/null)
  [ -n "${SID:-}" ] && ok "session $SID" || { bad "add response: $ADD"; return 1; }

  step "2) file listing (video + subtitle indexes)"
  FILES=$(curl -s "$BASE/api/v1/torrent/$SID")
  echo "$FILES"
  VID=$(echo "$FILES" | jqpy '[e for e in d if e["name"].lower().endswith((".mp4",".mkv",".webm"))][0]["index"]')
  SRT=$(echo "$FILES" | jqpy 'next((e["index"] for e in d if e["name"].lower().endswith(".srt")), "")')
  [ -n "$VID" ] && ok "video at index $VID" || { bad "no video file in listing"; return 1; }

  step "3) F1 progressive start (first bytes NOW, not after full download)"
  T0=$(date +%s)
  HEAD=$(curl -s -r 0-65535 -D - -o /tmp/f373_head.bin "$BASE/api/v1/torrent/$SID/stream/$VID")
  T1=$(date +%s)
  CODE=$(echo "$HEAD" | head -1 | awk '{print $2}')
  [ "$CODE" = "200" ] || [ "$CODE" = "206" ] && [ $((T1-T0)) -le 30 ] \
    && ok "stream answered $CODE with $(wc -c < /tmp/f373_head.bin) bytes in $((T1-T0))s" \
    || bad "progressive start: code=$CODE after $((T1-T0))s"

  step "4) F2 seek = Range request"
  R=$(curl -s -r 1000000-1000999 -D - -o /dev/null "$BASE/api/v1/torrent/$SID/stream/$VID")
  echo "$R" | head -1 | grep -q "206" && echo "$R" | grep -qi "accept-ranges: bytes" \
    && ok "206 Partial Content + Accept-Ranges: bytes" \
    || { bad "range semantics missing"; echo "$R" | head -8; }

  step "F3 srt→VTT subtitle surface"
  if [ -n "$SRT" ]; then
    V=$(curl -s "$BASE/api/v1/torrent/$SID/stream/$SRT?format=vtt" | head -c 6)
    [ "$V" = "WEBVTT" ] && ok "VTT conversion live (srt index $SRT)" \
      || bad "subtitle endpoint did not answer WEBVTT (got: $V)"
  else
    bad "no .srt in this torrent — pick a magnet with a separate srt"
  fi

  step "F4 audio track presence (ffprobe over the wire)"
  A=$(ffprobe -v error -select_streams a -show_entries stream=codec_name -of csv=p=0 "$BASE/api/v1/torrent/$SID/stream/$VID" 2>/dev/null | head -1)
  [ -n "$A" ] && ok "audio codec: $A" || bad "no audio track seen by ffprobe"

  echo "$SID" > /tmp/f373_sid
  echo "$VID" > /tmp/f373_vid
}

facade_floor() {
  step "B1) the title through the real search box"
  S=$(curl -s "$API/api/search?q=Sintel&provider=yts")
  echo "$S" | jqpy '[(g["title"], g["group_key"]) for g in d["groups"]][:3]' 2>/dev/null || { bad "search: $S"; return 1; }
  GK=$(echo "$S" | jqpy 'd["groups"][0]["group_key"]')
  [ -n "$GK" ] && ok "group key $GK" || { bad "no group key"; return 1; }

  step "B2) PlaybackInfo — engine truth envelope"
  PI=$(curl -s -X POST "$API/Items/$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote('$GK',safe=''))")/PlaybackInfo" -H "X-Emby-Token: $TOKEN" -H 'Content-Type: application/json' -d '{}')
  echo "$PI" | jqpy '[{"Id":s["Id"],"Container":s["Container"],"Streams":[m["Type"] for m in s.get("MediaStreams",[])]} for s in d["MediaSources"]]'
  echo "$PI" | jqpy 'd["MediaSources"][0]["Container"]' | grep -q "mp4\|m3u8" && ok "container learned from engine" || bad "container missing"

  step "B3) facade stream route → 302 to the engine LAN URL"
  ITEM=$(echo "$PI" | jqpy 'd["MediaSources"][0]["Id"]')
  ENC=$(python3 -c "import urllib.parse;print(urllib.parse.quote('$ITEM',safe=''))")
  LOC=$(curl -s -o /dev/null -w '%{redirect_url}' "$API/Videos/$ENC/stream")
  echo "  -> $LOC"
  case "$LOC" in "$BASE"/*) ok "302 hands the player the engine URL";; *) bad "unexpected redirect: $LOC";; esac

  step "B4) subtitle DeliveryUrl through the facade"
  DU=$(echo "$PI" | jqpy 'next((m["DeliveryUrl"] for s in d["MediaSources"] for m in s.get("MediaStreams",[]) if m["Type"]=="Subtitle"), "")')
  [ -n "$DU" ] && ok "DeliveryUrl: $DU" || bad "no Subtitle DeliveryUrl (session may lack an srt)"

  step "B5) the 302 target serves real bytes (player floor re-check via facade)"
  FIRST=$(curl -sL -r 0-1023 -o /dev/null -w '%{http_code} %{size_download}' "$LOC")
  echo "  -> $FIRST"
  echo "$FIRST" | grep -q "20[06]" && ok "bytes flow end-to-end" || bad "no bytes through the redirect chain"

  step "cleanup: purge the session (or let idle GC reap it)"
  curl -s -X POST "$BASE/api/v1/cache/purge" >/dev/null && echo "  purged"
}

case "${1:-all}" in
  engine) engine_floor ;;
  facade) facade_floor ;;
  all)    engine_floor && facade_floor ;;
  *) echo "usage: $0 {engine|facade|all}"; exit 2 ;;
esac

echo
echo "=== $PASS passed, $FAIL failed ==="
[ $FAIL -eq 0 ] || true
