# Providers

Every provider is a `BaseProvider` subclass in `cs_uk_api/providers/`,
registered in `_registry.py`. Adapters parse HTML / JSON only (no
headless JS): sites that generate their stream URL in a browser
JavaScript engine are reported as *not portable* by `scripts/gate.sh`
instead of being emulated.

## Registry

All 19 adapters are registered and surfaced via `GET /api/providers`
(verified by `tests/test_registry.py` — every provider module must be
registered). The table below details the first eight with
streaming-shape notes; the remaining eleven follow. Full per-provider
status: [`docs/provider-triage.md`](../docs/provider-triage.md).

| id          | site                          | sections                             | stream format |
|-------------|-------------------------------|--------------------------------------|---------------|
| uakino      | uakino.best                  | filmy, serials, animeukr, cartoons   | m3u8        |
| ufdub       | ufdub.com                     | filmy, serialy, doramy, cartoons, multserialy, anime | mp4 |
| unimay      | unimay.media (API)            | updates, projects                    | m3u8          |
| kinotron    | kinotron.tv                   | films, serials, cartoons, cartoon-series, anime | m3u8 |
| cikavaideya | cikava-ideya.top              | filmy, serialy, cartoon, arthaus     | m3u8          |
| hentaiukr   | hentaiukr.com                 | hentai                              | mp4           |
| bambooua    | bambooua.com                  | cinema, dorama, anime, lakorn, voice, tv-show, done, world-bl, now | mp4 |
| kinovezha   | kinovezha.tv                 | films, series, cartoons, s-cartoons  | m3u8          |
| uaflix      | uafix.net                     | filmy, serialy, doramy, cartoons, multserialy, anime | mp4 |
| animeua     | animeua.club                  | page, film, anime, ona, ova          | m3u8          |
| coaninet    | coani.net (API)               | films, series                        | m3u8          |
| klontv      | klon.fun                      | films, series                        | m3u8          |
| serialno    | serialno.tv                   | series                               | m3u8          |
| doramyworld | doramy.world                  | film, dorama, show                   | m3u8          |
| eneyida     | eneyida.tv                    | films, series                        | m3u8          |
| anitubeinua | anitube.in.ua                 | page                                 | m3u8          |
| simpsonsuatv | simpsonsua.tv                 | updates, page                        | m3u8          |
| animeon     | animeon.club                  | seasons, popular, page               | m3u8          |
| uaserialspro | uaserials.com                 | films, series, fcartoon, cartoons, anime, exclusive | m3u8 |

## Streaming shape

- **kinotron**: content page → `div.video-box iframe[data-src]` (ashdi
  player) → inline `file:` payload. Ashdi movie players return a direct
  `index.m3u8` URL; series players return a JSON array of seasons /
  episodes / dubs. Dead players (no files) keep the default Ukrainian
  translation instead of failing.
- **cikavaideya**: content page → `Player1` JSON → ashdi player page →
  `file: "https://.../index.m3u8"` (RegexExtractor). Requires
  `Referer: https://tortuga.wtf/`.
- **unimay**: JSON API (`https://api.unimay.media/v1/release/search`);
  the `title=` parameter is dead upstream — the adapter sends `query=`.
- **ufdub**: content page → `input_player=` → `video.ufdub.com` player
  page → `var a = [[title, 'mp4', mediaUrl]]` array. The player page is
  HTML, not media: the adapter resolves the second hop itself.
- **hentaiukr**: single JSON manifest (`/search/objects.json`); search
  is a case-insensitive substring on the Ukrainian title. Episodes come
  from `plur.cfg.json`; highest-quality source (1080 > 720 > 480) wins.
- **bambooua**: inline `const playlist` JSON on the content page; the
  stream URL is the resolved first group file (movies) or the requested
  `sNeM` episode.
- **kinovezha**: content page → player page with an obfuscated
  `file:"…"` value (upstream tor-decrypt) → direct `.m3u8` HLS stream.
- **uakino**: personal-use exception to the no-headless-JS rule: a warm
  headless-Chromium session serves every uakino.best request as an
  in-page `fetch()` (Cloudflare's silent per-request JS check never
  issues a cf_clearance cookie; see
  `docs/research/uakino-bypass-2026-08-02.md`). Streams are
  `ashdi.vip/vod/{id}` pages whose `file:'…m3u8'` lines yield direct
  `.m3u8` HLS; only that CDN hop uses plain httpx.

## Content ids

Search results carry ids like `kinotron:4519-duna`. Series episodes are
addressed as `<provider>:<external_id>:s1e2`; movie listings may append
`:__movie__`. Content ids may contain slashes (bambooua:
`dorama/722-story-of-kunning-palace`) — the API routes
(`/api/content/{content_id:path}`, `/api/stream/{content_id:path}`)
accept them.

## Live gate (2026-08-01)

`scripts/gate.sh <provider> [query]` is the canonical per-provider smoke test.
It runs search → content → stream → mpv (1 frame). The table below records
results from the latest full run; this documentation update did not re-run
live providers.

| provider    | gate | profile                          |
|-------------|------|----------------------------------|
| ufdub       | ✅   | h264 1280x720 1373kbps           |
| unimay      | ✅   | h264 720x480                     |
| kinotron    | ✅   | h264 1920x816                    |
| cikavaideya | ✅   | h264 1442x1080                   |
| hentaiukr   | ✅   | **hevc** 1280x720 — ⚠️ ps4-soft-decode-risk |
| bambooua    | ✅   | h264 1920x960                    |
| kinovezha   | ✅   | h264 1920x804                    |
| uakino      | ⛔   | public stance: known-broken — Cloudflare's per-request JS check blocks plain HTTP (research: `docs/research/uakino-reachability-2026-08-02.md`). Personal-use exception implemented (#51): headless-Chromium session + new-theme extraction, verified live (search/content/stream → playable m3u8, 2026-08-02) |

Gate queries: the default `Дюна` no longer matches upstream catalogs
that rotated their listings (cikavaideya, hentaiukr, bambooua) — their
rows above were re-gated with `фільм` / `школярки` / `квітка`. Search
itself still resolves on all of them; the query is content-dependent.

Non-H.264 output (e.g. hentaiukr HEVC) is flagged
`ps4-soft-decode-risk` (⚠️): mpv on PS4 decodes in software.

On mpv failure the gate captures the player HTML and scans it for
JS-generation markers (`eval(`, `Function(`, `atob(`, `obfuscated`).
A "not portable" verdict is issued only on real marker evidence;
otherwise the failure is reported as upstream/network.
