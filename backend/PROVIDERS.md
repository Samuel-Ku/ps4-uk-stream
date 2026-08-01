# Providers

Every provider is a `BaseProvider` subclass in `cs_uk_api/providers/`,
registered in `_registry.py`. Adapters parse HTML / JSON only (no
headless JS): sites that generate their stream URL in a browser
JavaScript engine are reported as *not portable* by `scripts/gate.sh`
instead of being emulated.

## Registry

All eight adapters are registered and surfaced via `GET /api/providers`:

| id          | site                          | sections                             | stream format |
|-------------|-------------------------------|--------------------------------------|---------------|
| uakino      | uakino.club (→ uakino.best)   | filmy, serials, animeukr, cartoons   | mp4           |
| ufdub       | ufdub.com                     | filmy, serialy, doramy, cartoons, multserialy, anime | mp4 |
| unimay      | unimay.media (API)            | updates, projects                    | m3u8          |
| kinotron    | kinotron.tv                   | films, serials, cartoons, cartoon-series, anime | m3u8 |
| cikavaideya | cikava-ideya.top              | filmy, serialy, cartoon, arthaus     | m3u8          |
| hentaiukr   | hentaiukr.com                 | hentai                              | mp4           |
| bambooua    | bambooua.com                  | cinema, dorama, anime, lakorn, voice, tv-show, done, world-bl, now | mp4 |
| kinovezha   | kinovezha.com                 | films, series, cartoons, s-cartoons  | mp4           |

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
- **kinovezha / uakino**: HTML content pages with embedded MP4 URLs.

## Content ids

Search results carry ids like `kinotron:4519-duna`. Series episodes are
addressed as `<provider>:<external_id>:s1e2`; movie listings may append
`:__movie__`. Content ids may contain slashes (bambooua:
`dorama/722-story-of-kunning-palace`) — the API routes
(`/api/content/{content_id:path}`, `/api/stream/{content_id:path}`)
accept them.

## Live gate (2026-08-01)

`scripts/gate.sh` runs search → content → stream → mpv (1 frame) per
provider and records a playability profile (codec / resolution / bitrate)
with `ffprobe`. Results of the latest full run:

| provider    | gate | profile                          |
|-------------|------|----------------------------------|
| ufdub       | PASS | h264 640x360–1280x720            |
| unimay      | PASS | h264 720x480                     |
| kinotron    | PASS | h264 1920x816                    |
| cikavaideya | PASS | h264 1442x1080                   |
| hentaiukr   | PASS | **hevc** 1920x1080 — soft-decode risk on PS4 |
| bambooua    | PASS | h264 1920x800                    |
| kinovezha   | PASS | h264 1920x804                    |
| uakino      | FAIL | site moved to uakino.best behind Cloudflare (upstream, not portability) |

Non-H.264 output (e.g. hentaiukr HEVC) is flagged
`ps4-soft-decode-risk`: mpv on PS4 decodes in software.

On mpv failure the gate captures the player HTML and scans it for
JS-generation markers (`eval(`, `Function(`, `atob(`, `obfuscated`).
A "not portable" verdict is issued only on real marker evidence;
otherwise the failure is reported as upstream/network.
