# Torrent lane operations — BitPlay engine + YTS catalog (spec #374)

The English-content lane adds two moving parts beside the Ukrainian
providers: the `yts` catalog provider (plain HTTPS against the YTS v2
API — no local service) and **BitPlay**, a self-hosted Go engine on the
LAN host that takes a magnet, progressively downloads, remuxes
MKV→MP4 on the fly (ffmpeg stream-copy), converts external `.srt`
subtitles to VTT, and serves a Range-capable LAN URL to the player.

This runbook covers first-run setup, health interpretation,
maintenance, troubleshooting, and the on-device verification recipe.
Engine facts below are verified against the live `ghcr.io/aculix/
bitplay:latest` image (research #367, 2026-08-25). Deploy materials:
`backend/deploy/docker-compose.bitplay.yml`; backend deploy context:
`backend/deploy/DEPLOY.md`.

## 1. First-run setup

### Stand up the engine

```bash
cd backend/deploy
mkdir -p config torrent-data   # config MUST exist before the first up
docker compose -f docker-compose.bitplay.yml up -d
curl -s http://localhost:3347/api/v1/capabilities
# {"ffmpeg":true,"version":"6.1.2"}   ← remux works out of the box
```

Settings persist in `./config/settings.json`; active downloads land in
`./torrent-data`. The default stanza uses bridge networking; if starts
feel slow (few peers), switch to the host-networking variant documented
at the bottom of the compose file.

### Wire the backend

The lane is disabled unless the engine URL is set (`CS_UK_TORRENT_ENGINE_URL`,
per the config module). Unset/empty ⇒ the lane is off and says so
loudly — it never silently pretends to stream. Add to
`cs-uk-api.service` next to the other env knobs:

```ini
Environment=CS_UK_TORRENT_ENGINE_URL=http://192.168.2.166:3347
# Only when BitPlay auth is enabled — both sides need BOTH values:
# Environment=CS_UK_TORRENT_ENGINE_USER=operator
# Environment=CS_UK_TORRENT_ENGINE_PASSWORD=change-me
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart cs-uk-api
```

### Where magnets come from

Nowhere manual in normal viewing: search flows through the same
facade/search box as every Ukrainian provider, the `yts` provider
carries per-quality magnet hashes from its listings and picks the
best-seeded suitable quality server-side, then the backend ensures an
engine session and hands the player BitPlay's LAN URL directly:

```
http://<engine-host>:3347/api/v1/torrent/{sessionId}/stream/{fileIndex}
```

For testing by hand you can paste any magnet into BitPlay's web UI at
`:3347`, or drive it with curl (recipe in §5).

## 2. Health interpretation

Three failure classes with different signatures — check them in this
order when playback breaks:

| Failure | What you see | Why |
| --- | --- | --- |
| Catalog API down | `/api/providers` slides `yts` to `degraded` → `down` (with `last_error_at`); English titles vanish from search/browse while Ukrainian ones still work; drift sweep flags it | Listing calls fail through the usual upstream guard |
| Engine down | Catalog stays healthy — posters and search work fine; pressing play fails fast with the typed error `unreachable` | The engine is only touched at play time; catalog health never probes it |
| Zero seeders / dead torrent | Play fails with an item-level rejection (`not_found` class) after the engine gives up fetching metadata (≤3 min); other titles unaffected | Swarm-level failure of THIS torrent, deliberately distinct from a dead lane |

```bash
curl -s http://127.0.0.1:8003/api/providers | grep -A4 '"id":"yts"'
curl -s http://127.0.0.1:8003/api/health          # all_down + warm state
curl -s http://192.168.2.166:3347/api/v1/sessions # engine alive + sessions
```

Rule of thumb: healthy `yts` status plus failing playbacks ⇒ look at
the engine, not the catalog.

## 3. Maintenance

- **Idle GC** — sessions idle for more than 15 minutes are reaped by
  the engine automatically (a stream that is actively open keeps its
  session alive). There is NO per-session delete endpoint; that is
  upstream reality, not something we skipped.
- **Global purge** — stops ALL sessions and wipes torrent data:

  ```bash
  curl -X POST http://localhost:3347/api/v1/cache/purge
  # {"message":"...","freedBytes":129564535}
  ```

  The same button lives in the web UI → Settings → Maintenance.
- **Disk location** — whatever directory you ran compose from:
  `./torrent-data` (mounted at `/app/torrent-data`). Check with
  `du -sh backend/deploy/torrent-data`. Bounds come from the GC
  cadence, not a quota — purge manually after heavy viewing weeks.
- **Remux cap semantics** — `BITPLAY_MAX_REMUX` (default 3) caps
  concurrent ffmpeg remux passes. Full ⇒ new remux requests get 503
  until a slot frees; native mp4/webm streams are NOT affected.
- **Logs without digging** — `curl -s http://localhost:3347/api/v1/logs`
  (ring buffer, last 500 lines) or `docker logs bitplay`.

## 4. Troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| Play fails instantly with `unreachable` | Engine down, wrong `CS_UK_TORRENT_ENGINE_URL`, container crash-looping | `docker compose -f backend/deploy/docker-compose.bitplay.yml ps`; `curl -s localhost:3347/api/v1/capabilities`; fix address/compose, restart backend |
| Play hangs ~minutes, then item-level `not_found` | Dead torrent: zero seeders / metadata timeout (engine blames this magnet: 400/504) | Try another title or quality; check the peers column in the BitPlay UI. Not a lane fault |
| 401 from the engine in backend logs | Auth enabled on one side only | Both sides need complete pairs: `BITPLAY_AUTH_USERNAME`+`PASSWORD` AND `CS_UK_TORRENT_ENGINE_USER`+`PASSWORD`, identical values |
| Remuxed MKV plays but will not seek | Current engine limit: the remux path serves chunked progressive fMP4 with `Accept-Ranges: none` — no HTTP Range support | Expected behaviour, not a bug. Native-container files (`/stream/`) seek normally; restarting playback is the workaround for MKV sources |
| No subtitles on an MKV | Current engine limit: remux strips embedded subtitle tracks; ONLY `.srt` files present as separate torrent files convert to VTT | Pick releases carrying external `.srt` files if subtitles matter; nothing else will render for MKV sources today |
| New MKV refuses with 503 mid-evening | All remux slots busy (`BITPLAY_MAX_REMUX`) | Wait and retry; raise the cap in the compose file if several MKVs play concurrently |
| Starts slow, peers ≈ 0–1 | Per-torrent random listen ports blocked behind docker NAT | Switch to the host-networking variant at the bottom of the compose file; verify tracker reachability in the UI |
| English titles gone from search entirely | Catalog API down, engine irrelevant | Check `yts` in `/api/providers` (§2); restart backend if stuck down |

## 5. On-device verification recipe

Distilled from the prototype sweep (research #367). Run once after
first deploy and after any engine change. Prereqs: engine up (§1),
backend running with `CS_UK_TORRENT_ENGINE_URL` set, PS4 with Switchfin
pointed at the backend.

```bash
BASE=http://192.168.2.166:3347
MAGNET="magnet:?xt=urn:btih:08ada5a7a6183aae1e09d831df6748d566095a10&dn=Sintel&tr=udp%3A%2F%2Fexplodie.org%3A6969&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337"

# 1) Add the Sintel test torrent — blocks until metadata arrives
#    (~seconds on a well-seeded torrent)
SID=$(curl -s -X POST $BASE/api/v1/torrent/add \
      -H 'Content-Type: application/json' \
      -d "{\"Magnet\": \"$MAGNET\"}" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["sessionId"])')
echo $SID   # 40-char infohash hex

# 2) List files, note the video's index (largest video-like file)
curl -s $BASE/api/v1/torrent/$SID
# [{"index":5,"name":"Sintel.mp4","size":129241752}, ...]

# 3) Poll progress until bytes flow (complete climbs toward size)
watch -n5 "curl -s $BASE/api/v1/sessions"

# 4) Take the stream URL and Range-check it
curl -s -r 0-1023 -D - $BASE/api/v1/torrent/$SID/stream/5 -o /dev/null
# expect HTTP/1.1 206 Partial Content + Accept-Ranges: bytes
```

Then on the PS4:

1. Open Switchfin → search «Sintel» → open the card (the English title
   interleaves with Ukrainian results in the same search screen).
2. Press play — the facade route ensures the engine session and hands
   the player the engine URL from step 4. Video must START
   progressively within seconds, no full-download wait.
3. Seek forward and backward — jumps land near-instantly (Range works;
   if the source had been MKV, seeking would be unavailable per §4).
4. Subtitles — Sintel's separate `.srt` track appears and renders as
   VTT.
5. Clean up: `curl -X POST $BASE/api/v1/cache/purge` — or let the
   15-minute idle GC reap the session.

Any failed step maps through §4 before re-testing.
