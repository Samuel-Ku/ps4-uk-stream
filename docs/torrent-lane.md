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
# Engine liveness probe cadence (§2): seconds between ticks, default 300,
# 0 disables. A split auth pair above pins yts:engine down at startup.
# Environment=CS_UK_ENGINE_PROBE_INTERVAL=300
# English SERIES (#379): the Popcorn shows host. Every known public
# host is dead (research #366) — self-host popcorn-api or point at a
# live mirror. Unset ⇒ movies work, series say `unreachable` loudly.
# Recipe: §1 «Self-host the series host (popcorn-api)» below.
# Environment=CS_UK_POPCORN_BASE_URL=http://popcorn.lan:9000
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

**Series (#379)** ride the same magnet path one level deeper: a show
opened from search/home resolves its season through the Popcorn shows
host (`CS_UK_POPCORN_BASE_URL`, unset ⇒ series browsing answers the
loud `unreachable` verdict while movies stay live), episodes carry the
canonical `:sNeM` ids, and an episode's `stream()` picks the season
torrent (best-seeded suitable quality) and selects the episode's file
index inside it — the player still receives nothing but the engine's
LAN URL. Resume/NextUp work per episode via the canonical ids.

**Subtitles and audio (#378)** ride the same session automatically: when
the torrent carries an external `.srt` (BitPlay converts it to VTT on
request), PlaybackInfo grows a `Subtitle` media-stream whose
`DeliveryUrl` is the facade's `/Stream/{item}/vtt` — a 302 to the
engine's `stream/{i}?format=vtt`, so the player never needs the raw LAN
host. Selectable audio surfaces as `Audio` media-stream entries the same
way. A session with neither (no separate srt, one audio track) leaves
PlaybackInfo byte-identical to the pre-#378 shape — the Ukrainian lane
never changes.

### Self-host the series host (popcorn-api)

Series discovery is the one dependency of the lane with **no live
public host** (research #366, probed 2026-08-25: `api.popcorntime.app`
is a marketing site, `*.api-fetch.sh` is DNS-dead, `popcorn-ru.tk` sits
behind an anti-bot wall). The backend's knob (`CS_UK_POPCORN_BASE_URL`)
points at whatever host speaks the contract — self-hosted or a mirror.
Unset ⇒ movies keep working and series answer the loud typed verdict;
nothing breaks silently.

**What the host must serve** (the acceptance bar — any candidate is
tested against exactly these; shapes per `providers/popcorn.py` and
the byte-true fixtures in `cs_uk_api/tests/fixtures/yts/`):

| Route | Returns |
| --- | --- |
| `GET /shows/{page}?sort=updated&order=-1` | JSON array of show objects (`null` = empty page) — home "newest" rail |
| `GET /shows/{page}?sort=name&keywords=<query>` | same shape — search |
| `GET /show/{imdb_id}` | one show object (see below) |

The show object carries `imdb_id`, `tvdb_id`, `title`, `year` (STRING),
`slug`, `description`, `num_seasons`, `genres`, `images.poster`,
`rating.percentage`, and `episodes[]` with `season`, `episode`,
`title`, `overview` and a **per-episode quality map**:
`torrents: {"720p": {"url": "magnet:?xt=…", "seeds": N}, …}` — quality
`"0"` means unknown. Episode magnets are used VERBATIM (popcorn
desktop's tv.js contract); the backend never rebuilds them from hashes.
`series_show_tt8740758.json` (Chernobyl) in the fixtures directory is a
complete example.

**Option A — self-host popcorn-official/popcorn-api** (MIT, Node ≥6.3,
MongoDB + `mongoimport`, gulp build; this is the implementation the
dialect was documented from):

```bash
git clone https://github.com/popcorn-official/popcorn-api.git
cd popcorn-api && npm install && gulp build
# MongoDB must be reachable; run `mongod` and note the api's port (default per its config)
npm start   # or the built entry point; keep it on a LAN port, e.g. 9000
```

> **Honest limitation:** the upstream ships the API, **not a catalog** —
> its database starts empty (its CLI populates metadata from Trakt;
> episode magnets came from provider scrapers that died with the
> 2017-era public servers). An empty deployment answers `200 []` —
> alive but useless. Self-hosting is only the whole fix if you also
> source catalog data (community scrapes of that era exist but none is
> maintained or clearnet-listed as of research #366).

**Option B — point at a live mirror.** Any host — a community deployment
or a friend's LAN box — that passes the acceptance table above works:
set the knob, verify with the curls below, done. This is the cheap path
while it lasts; mirrors churn (§5 of research #366).

**Wire it** (same unit as the other knobs):

```ini
Environment=CS_UK_POPCORN_BASE_URL=http://<host>:9000
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart cs-uk-api
```

The configured host joins the yts provider's `safe_get` allowlist
automatically (seeded from the knob at construction) — no code or
declaration change needed.

**Verify:**

```bash
curl -s "http://<host>:9000/shows/1?sort=updated&order=-1" | head -c 300
curl -s "http://<host>:9000/show/tt8740758" | python3 -m json.tool | head -20
# then through the backend — the show must appear in search:
curl -s "http://127.0.0.1:8003/api/search?q=chernobyl" | grep -c chernobyl
```

**Failure signatures:** a dead or wrong series host does NOT take the
English lane down — search degrades to movies-only with a
`yts series search degraded` warning in the backend log, and series
browse/play answers the typed not-configured/`unreachable` verdict.
Playback-surface failures against the host do record against `yts`'s
sliding window like any lane fault (§2). Movies are never affected.

## 2. Health interpretation

Three failure classes with different signatures — check them in this
order when playback breaks:

| Failure | What you see | Why |
| --- | --- | --- |
| Catalog API down | `/api/providers` slides `yts` to `degraded` → `down` (with `last_error_at`); English titles vanish from search/browse while Ukrainian ones still work; drift sweep flags it | Listing calls fail through the usual upstream guard |
| Engine down | `yts:engine` slides `degraded` → `down` on its own row (spec #394); catalog and search keep working; pressing play fails fast with the typed error `unreachable` (a typed 404 through the facade) | The engine's liveness has its own tracked entry — see «The `yts:engine` entry» below |
| Zero seeders / dead torrent | Play fails with an item-level rejection (`not_found` class) after the engine gives up fetching metadata (≤3 min); other titles unaffected; **`yts:engine` stays healthy** | Swarm-level failure of THIS torrent, deliberately distinct from a dead lane AND from a dead engine |

### The `yts:engine` entry

When the engine URL is configured, `/api/providers` and `/api/health`
carry a separate `yts:engine` row ("BitPlay engine") so «dead catalog
API» and «dead engine» are distinguishable at a glance. Unconfigured ⇒
the row simply does not exist.

What moves it: **engine-process unreachability only**, from two sample
sources — the background liveness probe and stream-time
`EngineUnavailable` faults (a play attempt that cannot reach the
engine records against `yts:engine`, never against `yts`; a dead swarm
is an item-level `not_found` and never samples this row). The same
sliding-window thresholds apply as for any provider: `degraded` is the
early-warning tier, `down` is a persistently unreachable engine.

- **The probe.** Every `CS_UK_ENGINE_PROBE_INTERVAL` seconds (default
  300; `0` disables the loop) the backend pings the engine's
  capabilities endpoint with a 5s timeout. **Any HTTP answer —
  200/401/403/5xx — counts as alive**; only transport death (refused,
  timeout, DNS) is a failure sample. It is a process-liveness check,
  not a deep-health check, and it needs no engine-side changes.
- **The misconfiguration marker.** A HALF-configured engine — the URL
  set but the auth pair split (user without password or vice versa),
  or a schemeless URL — pins `yts:engine` `down` deterministically at
  startup with zero samples. Fix the env pair (BOTH values or neither —
  BitPlay engages auth only for the pair) and restart the backend.
- **The client is never reset by it.** The engine is LAN-local; the
  entry is deliberately NOT part of the watchdog's all-down set, so a
  dead engine alone (e.g. during a WAN outage, when the engine stays
  reachable) never resets the client.

```bash
curl -s http://127.0.0.1:8003/api/providers | grep -A4 '"id":"yts"'
curl -s http://127.0.0.1:8003/api/providers | grep -A4 'yts:engine'
curl -s http://127.0.0.1:8003/api/health          # all_down + warm state
curl -s http://192.168.2.166:3347/api/v1/sessions # engine alive + sessions
```

Rule of thumb: healthy `yts` plus failing playbacks used to mean
"probably the engine" — now the `yts:engine` row answers it directly:
`degraded`/`down` there is the engine, `ok` there with failing plays is
this title's swarm.

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
| Play fails instantly with `unreachable` | Engine down, wrong `CS_UK_TORRENT_ENGINE_URL`, container crash-looping | Check `yts:engine` in `/api/providers` (§2); `docker compose -f backend/deploy/docker-compose.bitplay.yml ps`; `curl -s localhost:3347/api/v1/capabilities`; fix address/compose, restart backend |
| `yts:engine` down at startup, never played | Half-configured engine: auth pair split (one env var set) or schemeless URL | Set BOTH `CS_UK_TORRENT_ENGINE_USER`/`PASSWORD` or neither; give the URL an `http(s)://` scheme; restart backend |
| Play hangs ~minutes, then item-level `not_found` | Dead torrent: zero seeders / metadata timeout (engine blames this magnet: 400/504) | Try another title or quality; check the peers column in the BitPlay UI. Not a lane fault |
| 401 from the engine in backend logs | Auth enabled on one side only | Both sides need complete pairs: `BITPLAY_AUTH_USERNAME`+`PASSWORD` AND `CS_UK_TORRENT_ENGINE_USER`+`PASSWORD`, identical values |
| Remuxed MKV plays but will not seek | Current engine limit: the remux path serves chunked progressive fMP4 with `Accept-Ranges: none` — no HTTP Range support | Expected behaviour, not a bug. Native-container files (`/stream/`) seek normally; restarting playback is the workaround for MKV sources |
| No subtitles on an MKV | Current engine limit: remux strips embedded subtitle tracks; ONLY `.srt` files present as separate torrent files convert to VTT | Pick releases carrying external `.srt` files if subtitles matter; nothing else will render for MKV sources today. When a separate srt IS present, PlaybackInfo surfaces it automatically (#378) as a Subtitle track via `/Stream/{item}/vtt` |
| New MKV refuses with 503 mid-evening | All remux slots busy (`BITPLAY_MAX_REMUX`) | Wait and retry; raise the cap in the compose file if several MKVs play concurrently |
| Starts slow, peers ≈ 0–1 | Per-torrent random listen ports blocked behind docker NAT | Switch to the host-networking variant at the bottom of the compose file; verify tracker reachability in the UI |
| English titles gone from search entirely | Catalog API down, engine irrelevant | Check `yts` in `/api/providers` (§2); restart backend if stuck down |

## 5. On-device verification recipe

Distilled from the prototype sweep (research #367). Run once after
first deploy and after any engine change. Prereqs: engine up (§1),
backend running with `CS_UK_TORRENT_ENGINE_URL` set, PS4 with Switchfin
pointed at the backend.

> The protocol below has an EXECUTABLE form since the #373 acceptance:
> `backend/deploy/accept_373.sh {engine|facade|all}` runs steps 1–4 plus
> the player-floor checks (progressive start, Range seek, srt→VTT,
> audio track) and the same title through the real backend, and
> `backend/deploy/run_backend_8003.sh` starts that backend on the
> Switchfin device port. The walkthrough remains the canonical
> explanation; the script is the one-command re-run. Findings:
> `docs/test-artifacts/accept-373-2026-09-05.md`.

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
