# Provider triage (issue #14)

Triage of the 20 content providers in `codeberg.org/CakesTwix/cloudstream-extensions-uk`.
Updated whenever a provider lands a `feat` commit in this repo; the GitHub
issue [#17](https://github.com/Samuel-Ku/ps4-uk-stream/issues/17) tracks the
implementation work, and individual providers graduate from "TBD" to "ready"
once they pass the live gate (search → content → stream → plays in mpv).

## Legend

- **Search**: how the provider's search box is implemented upstream.
- **Player**: how the provider's stream URL is resolved (iframe chain, regex on inline JS, JSON from a CDN player, or packed/obfuscated JS that we cannot port without a JS engine).
- **JS dep**: how much JS execution is needed to extract the stream URL.
  `none` = pure HTML scraping, `mild` = regex on inline JS, `heavy` = must
  execute JS in a real browser engine. uakino's `heavy` is served by a
  headless-Chromium session (Playwright) rather than a pure-Python port —
  it is the sole `heavy` provider and lands as `ready` (ADR-0004 amendment).
- **Verdict**:
  - `ready` — landed in `backend/cs_uk_api/providers/` and passing tests
  - `portable` — code is straightforward to port (HTML + regex/iframe), not yet started
  - `partial` — mixed; some content types resolve fine, others need JS
  - `not portable` — JS engine required, out of scope for v2

## Table

| Provider id | Upstream plugin | Kotlin sources | Search | Player | JS dep | Verdict |
| ----------- | --------------- | -------------- | ------ | ------ | ------ | ------- |
| uakino | [UakinoProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/UakinoProvider) | `UakinoProvider.kt` (14.6 KB) | HTML | iframe → regex (browser session) | heavy (browser session) | **ready** — heavy (browser session; headless Chromium via Playwright); warm on startup, heartbeat every 5 min, status="warming" while cold; see ADR-0002 / ADR-0004 amendments |
| uaflix | [UAFlixProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/UAFlixProvider) | `UAFlixProvider.kt` (14.9 KB) | HTML, has mainPage | iframe → regex | mild | **ready** |
| animeua | [AnimeUAProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/AnimeUAProvider) | `AnimeUAProvider.kt` (8.2 KB), `Tracker.kt` | HTML | iframe → JSON `file:` (dubs or m3u8) | mild | **ready** |
| kinovezha | [KinoVezhaProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/KinoVezhaProvider) | `KinoVezhaProvider.kt` (10.3 KB) | HTML | iframe → regex (torDecrypt) | mild | **ready** |
| banderakino | [BanderakinoProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/BanderakinoProvider) | `BanderakinoProvider.kt` (386 lines — **removed from codeberg master in commit `3f4641d`**; recovered from history for issue #241) | HTML | inline `player = new Playerjs({...})` JSON on the episode page → m3u8 (Referer `banderakino.online`); fallback `getM3url` regex | mild | **portable — NOT landed (upstream dead — re-probed again 2026-08-24: `banderakino.online` zone still resolves to Cloudflare edges `104.21.52.164`/`172.67.201.84` but the https origin times out on `/`, `/serialy`, `www.` (curl 000 at 15s); plain `http://` 301s to https then dies — same 522-class outage as 2026-08-16; `.com`/`.net`/`.org`/`.ua`/`.pp.ua` all NXDOMAIN; web search finds no replacement domain; control fetches to ufdub.com fine same minute → network healthy. Recommendation recorded on #241: wontfix unless the site returns)** |
| bambooua | [BambooUAProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/BambooUAProvider) | `BambooUAProvider.kt` (8.9 KB), `JSONModel.kt` | HTML + JSON | inline `const playlist` JSON on the content page | mild | **ready** (no_episodes fix per #139: a content page with an empty/missing `const playlist` — dead/removed listing or subscription-gated title («Для підписників» served without a manifest) — raises `gated` (ADR-0002) from content()/stream(), so `can_gate`'s `filter_gated_items` drops the zero-season card during `load_home`; id-collapse fix per #139: `/zhanr/` cards keep the full multi-segment path in the external_id (`zhanr/drama/N-slug`) so content()/stream() rebuild the verbatim 200 URL — the collapsed `drama/N-slug` form 301-redirects into a health-down `not_found`) |
| coaninet | [CoaninetProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/CoaninetProvider) | `CoaninetProvider.kt` (12.3 KB) | JSON API | pre-resolved HLS master | none | **ready** |
| klontv | [KlonTVProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/KlonTVProvider) | `KlonTVProvider.kt` (10.0 KB), `Tracker.kt` | HTML | iframe → regex | mild | **ready** |
| uaserialspro | [UASerialsProProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/UASerialsProProvider) | `UASerialsProProvider.kt` (20.5 KB) | HTML | AES-256-CBC + PBKDF2 + Tortuga XOR | mild (adds pycryptodome dep) | **ready** |
| eneyida | [EneyidaProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/EneyidaProvider) | `EneyidaProvider.kt` (20.6 KB) | HTML | iframe → PlayerJS JSON | mild | **ready** |
| anitubeinua | [AnitubeinuaProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/AnitubeinuaProvider) | `AnitubeinuaProvider.kt` (23.4 KB) | HTML | iframe → ashdi.vip + qeruya.cyou Referer; per-episode dub playlists | mild | **ready** |
| animeon | [AnimeONProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/AnimeONProvider) | `AnimeONProvider.kt` (56.7 KB) | JSON API + HTML | XOR-decoded iframe (moonOuterDecode + moonDecrypt, pure Python) → ashdi.vip direct m3u8 | mild (no JS engine; pure stdlib decode verified byte-exact) | **ready** |
| kinotron | [KinoTronProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/KinoTronProvider) | `KinoTronProvider.kt` (7.9 KB) | HTML | iframe → inline JSON | mild | **ready** |
| hentaiukr | [HentaiUkrProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/HentaiUkrProvider) | `HentaiUkrProvider.kt` (5.3 KB) | JSON manifest + plur.cfg.json | mp4 (per-source highest-quality pick) | none | **ready** (in scope per spec; no hiding) |
| doramyworld | [DoramyWorldProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/DoramyWorldProvider) | `DoramyWorldProvider.kt` (8.3 KB), `JSONModel.kt` | HTML + JSON | ashdi.vip iframe → data-player JSON | mild | **ready** |
| cikavaideya | [CikavaIdeyaProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/CikavaIdeyaProvider) | `CikavaIdeyaProvider.kt` (8.2 KB) | HTML | regex (ashdi.vip `file:`) | mild | **ready** (gated fix per #139: removed/trailer-only/no-player titles raise `gated` (ADR-0002), dead ashdi embed raises `gated`; `can_gate=True` so `filter_gated_items` drops them from home) |
| ufdub | [UFDubProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/UFDubProvider) | `UFDubProvider.kt` (6.1 KB) | HTML | iframe → regex | none | **ready** |
| unimay | [UnimayProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/UnimayProvider) | `UnimayProvider.kt` (6.6 KB) | JSON | hls.master URL | none | **ready** |
| serialno | [SerialnoProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/SerialnoProvider) | `SerialnoProvider.kt` (10.1 KB) | HTML | iframe → torDecrypt | mild | **ready** |
| simpsonsuatv | [SimpsonsUATvProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/SimpsonsUATvProvider) | `SimpsonsUATvProvider.kt` (29.3 KB) | HTML | iframe → ashdi.vip | mild | **ready** (multi-iframe selection + SSRF redirect guard; season cap kept per #138 audit) |

## Episode-rail verification sweep (#136 -> #139)

The episode-rail sweep (`backend/cs_uk_api/sweep_episode_rail.py`, see issue
#136) walks the series play path for every provider -- `/Shows/{g1}/Seasons`
-> `/Shows/{g1}/Episodes?seasonId=` -> `POST /Items/{ep}/PlaybackInfo` ->
`GET /Videos/{ep}/stream` -- and asserts 200 at each hop. Issue #139 targets
every provider the sweep flags broken.

**Status (verified 2026-08-09, live sweep against a warm `/api/home`):** the
adapter fixes #139 set out to land are **already implemented and
regression-tested** in this branch. The documented pre-fix bugs are closed:

| Target (from #139) | Bug | State | Locked by |
| ------------------ | ---- | ------ | --------- |
| kinotron | BUG-1 -- `stream()` choked on the production 2-part id `slug:sNeM` | **fixed** | `test_kinotron_stream_selects_requested_episode` |
| serialno | BUG-2 -- Tortuga payload drift (`_season_list` flat vs dub-wrapped; `{label}`/`(subtitle:)` trim) | **fixed** | `serialno` content/stream tests |
| bambooua | gated titles leak into the catalog | **fixed** | `can_gate=True` + `filter_gated_items` drops `const playlist = []` cards |
| cikavaideya | gated / dead-embed titles | **fixed** | `can_gate=True`; trailer-only / empty `Object({})` / dead ashdi raise `gated` |
| animeua / simpsonsuatv | episode-rail for non-gated series | **fixed** | existing content/stream tests green |

The remaining sweep flags are **not adapter bugs** -- they are live-data /
cache edge cases, confirmed by direct reproduction:

- **Orphaned / stale catalog ids** -- e.g. `cikavaideya:g1:16cd3e36c59d5348`
  (bug item_unavailable) resolves to a 404 upstream page and fails the slug
  regex; the id was valid when cached and is now dead. Correct 404, not a
  regression.
- **Gated series leaked via home-cache staleness** -- e.g. `bambooua:...personasulli`
  (warn no-eps) serves `const playlist = []`. `filter_gated_items` *does* drop
  it on a fresh home build (verified in isolation); it only appears while a
  stale 30-min home snapshot predates the upstream gating. Self-heals on TTL.
- **Upstream mis-tags** -- e.g. an animeua episode page carries the
  `Povnometrazhka` genre and a `/vod/` (not `/serial/`) player, so
  `_type_from_player` classifies it as a film; the catalog lists it as a
  series. The adapter faithfully mirrors the upstream `tvType` -- forcing it
  to series would misclassify genuine anime films.
- **Volatile snapshot** -- the warm home rotates, so the *same* provider flips
  between ok and warn run-to-run (e.g. `uaflix` was ok in one run and warn
  no-eps in the next). The flags track which specific series landed in the
  snapshot, not a stable adapter defect.

**Conclusion for #139:** the actionable adapter work is complete; the residual
warn/bug rows are upstream/live artifacts that cannot be fixed at the adapter
layer without breaking legitimate titles, and most self-heal as catalog caches
rotate. Acceptance "every non-gated series episode-rail returns 200" holds for
genuinely non-gated series; the flagged rows are gated, dead, or mis-tagged
upstream.

## Degraded-provider triage (2026-08-15, spec #298 ticket #302)

The health monitor (`/api/providers`) reported `degraded` for kinotron,
cikavaideya, and simpsonsuatv (≥40% errors in the 20-sample sliding
window). Live-gate triage (`scripts/gate.sh` — search → content → stream
→ mpv plays 1 frame) on 2026-08-15:

| Provider | Gate result | Verdict |
| -------- | ----------- | ------- |
| kinotron | ✅ PASS — «Дюна: Пророцтво» plays (h264 3840x1920); first title's stream dead, retry loop passed | **transient** |
| cikavaideya | ✅ PASS first try — «Жінка з вітрини» plays (h264 1442x1080) | **transient** |
| simpsonsuatv | ✅ PASS first try — «Сімпсони» plays (h264 1920x1080, episode-fallback by design) | **transient** |

All three recorded `last_error_at = 2026-08-15T00:27:29+00:00` — the
backend's ~00:26 restart (pid 1692410); the concurrent background warm
tripped upstream rate-limit/timeout spikes that filled the window.
anitubeinua had the identical burst (00:27:23) and already self-healed
back to `ok`. No provider fails reproducibly → **no follow-up issue
filed, no adapter changes** (spec #298: passing probe = transient drift;
rate-limit/timeout results are not filed).

Title-level note (not provider-wide, deliberately not filed): kinotron
`4519-duna` («Дюна») reproducibly yields no stream — its ashdi.vip
embed (`/vod/33957`) serves a 47-byte «Файл не знайдено» page upstream.
The adapter correctly follows kinotron → ashdi and finds nothing to
parse; this is the documented dead-embed/gated condition (ADR-0002), and
the gate's by-design top-hit retry (issue #39) covers it.

## Degraded-provider re-check (2026-08-16, spec #298)

Follow-up to the 2026-08-15 verdicts above: the SAME three providers
(kinotron, cikavaideya, simpsonsuatv) hit a fresh degraded window on
2026-08-16 — detected by the nightly drift sweep, which had been
scheduled under spec #298 (ticket #300).

| Provider | Window | Root cause | Verdict |
| -------- | ------ | ---------- | ------- |
| kinotron | 03:10–13:42 UTC | shared origin `91.240.20.12` unreachable (TCP connect timeout, 100% ping loss) | **transient** — recovered |
| cikavaideya | 03:10–13:42 UTC | same shared origin | **transient** — recovered |
| simpsonsuatv | 03:10–13:42 UTC | same shared origin | **transient** — recovered |

Details:

- All three domains (`kinotron.tv`, `cikava-ideya.top`, `simpsonsua.tv`)
  resolve to the SAME origin `91.240.20.12` (confirmed via Google DoH —
  not a local DNS artifact). The origin answered no ping and no TCP on
  port 80/443 from the backend host; control checks to other providers
  (`kinovezha.tv` → 301) were fine, and the sibling domain
  `kinotron.com.ua` (`89.184.75.80`) stayed up.
- The drift sweep at 03:10 passed all three (healthy baseline), the
  13:36 run failed them (first consecutive failure — counter 0→1), and
  the 13:42 run passed again (counter reset to 0, baseline refreshed).
  The failure never reached the two-consecutive threshold, so **no issue
  was filed** and the monitor self-healed — exactly the spec #298 design
  (single failures are not drift; the counter/window self-heals).
- No adapter changes: the adapters' parsing is unaffected (healthy
  signatures `count 18/18/4` restored identically on recovery); the
  outage was pure upstream unavailability.

## Drift incident: ufdub failing sweeps (2026-08-24, issue #357)

The nightly drift monitor (spec #285) failed `ufdub` two sweeps
consecutively and filed #357. Root cause was NOT upstream content
change and NOT the deep probe (ufdub was not even in that night's deep
rotation; manual deep probes of movie AND series cards pass:
content → stream → HEAD 200). The verdict was the listing signature's
style band: `style 'dorama' left the band (0.25 → 0.00)`.

Root cause in the ADAPTER: ufdub category pages append a rotating
`div.section` widget («recently updated» cross-listed
serials/anime/doramas) after the real `div.floaters.grid-thumb`
category grid — every one of the six category pages carries it (12
grid cards + 4 widget cards, verified live on all six on 2026-08-24).
`browse()` selected `.short-text` globally and over-captured the
widget, despite its own comment claiming the upstream Kotlin's
«remove `.section` blocks» behavior. Consequences:

- The drift baseline calibrated ON the polluted listing (16 cards,
  styles `{anime: 0.75, dorama: 0.25}` at calibration time).
- The widget tail rotates as episodes land (dorama → anime observed
  between calibration and 2026-08-24), so any style occupying it can
  leave the ≥20% significance band → permanent verdict flapping.

Fix (#357): `browse()` now skips cards under a `div.section`,
implementing the documented Kotlin-parity exclusion. Search is
untouched (search-page results genuinely live inside such a block);
pagination lives inside the grid container, so `has_next` is
unaffected. Post-fix listing = the true grid (12 films on `/film/`,
forms all-movie, styles empty — stable across rotations); fixture
`film_listing.html` re-captured live 2026-08-24 with the widget
present as regression evidence.

Deploy companion (operator step, runtime state only): the persisted
baseline still carries the stale `[16,16]` count band, so the first
post-fix sweep trips `count 12 below calibrated low 16` until the
signature recalibrates — the baseline only refreshes on healthy
passes (chicken-and-egg for permanent composition changes). ufdub's
entry was dropped from this host's `~/.cache/cs-uk-api/drift-state.json`;
the next sweep calibrates fresh ([12,12], `style_frac {}`) and the
healthy pass comments + closes #357 per the monitor's recovery flow.
Verified end-to-end against live upstream with an isolated temp state:
first run `ok=True, reason="baseline (first calibration)"`.

## Per-provider "owner" field (suggested order)

Group 1 (simple, HTML-based, `<15 KB`):

1. `ufdub` — landed in `backend/cs_uk_api/providers/ufdub.py` (10 tests, ready)
2. `unimay` — landed in `backend/cs_uk_api/providers/unimay.py` (13 tests, ready; JSON API, not HTML)
3. `kinotron` — landed in `backend/cs_uk_api/providers/kinotron.py` (9 tests, ready)
4. `cikavaideya` — landed in `backend/cs_uk_api/providers/cikavaideya.py` (24 tests, ready; #139 gated fix: a removed title (`.fmessage` «Видалено на прохання правовласника»), a trailer-only `{"Трейлер": youtube}` Player1, or an empty `Object({})` raises `gated` from `content()`, and a dead ashdi embed (`<center>Файл не знайдено</center>`) raises `gated` from `stream()` — ADR-0002 deliberate-unavailability verdicts, no health impact; `can_gate=True` drops those cards from the catalog during `load_home`)
5. `animeua` — landed in `backend/cs_uk_api/providers/animeua.py` (9 tests, ready; supports `translations_level="episode"`)
6. `uaflix` — landed in `backend/cs_uk_api/providers/uaflix.py` (21 tests, ready; uses shared `RegexExtractor`)
7. `kinovezha` (landed, 17 tests, ready); `bambooua` — landed in `backend/cs_uk_api/providers/bambooua.py` (22 tests, ready; #139 gated fix: an empty/missing `const playlist` — dead/removed listing or subscription-gated title — raises `gated` (ADR-0002) from `content()`/`stream()`, so `filter_gated_items` drops the zero-season card during `load_home`)
8. `coaninet` — landed in `backend/cs_uk_api/providers/coaninet.py` (8 tests, ready; JSON API, single-hop stream)

Group 2 (medium, 10–25 KB):

1. `klontv` — landed in `backend/cs_uk_api/providers/klontv.py` (10 tests, ready; DLE + ashdi.vip iframe chain)
2. `serialno` — landed in `backend/cs_uk_api/providers/serialno.py` (12 tests, ready; homepage-as-listing, tortuga.tw iframe + torDecrypt — code-duplication with KinoVezha noted as follow-up)
3. `banderakino` — skipped, live site offline (HTTP 522)
4. `doramyworld` — landed in `backend/cs_uk_api/providers/doramyworld.py` (16 tests, ready; WordPress + ashdi.vip iframe + data-player JSON DTOs)

Group 3 (heavy, needs deeper triage):

1. `eneyida` — landed in `backend/cs_uk_api/providers/eneyida.py` (12 tests, ready; DLE + PlayerJS JSON season/episode playlists)
2. `uaserialspro` — landed in `backend/cs_uk_api/providers/uaserialspro.py` (20 tests, ready; AES-256-CBC + PBKDF2 player-config decrypt + Tortuga XOR; adds `pycryptodome>=3.20` dep; Tortuga XOR + AES helpers extracted to shared `_tortuga.py` / `_crypto.py` modules used by serialno/kinovezha too)
3. `anitubeinua` — landed in `backend/cs_uk_api/providers/anitubeinua.py` (19 tests, ready; DLE + ashdi.vip iframe + `qeruya.cyou` Referer; supports `translations_level="episode"`; per-episode studio dubs from playlist JSON; 764 lines)
4. `simpsonsuatv` — landed in `backend/cs_uk_api/providers/simpsonsuatv.py` (21 tests, ready; DLE + ashdi.vip iframe; 2 sections (`updates` carousel + `multserialy-ukrainskoyu` listing); multi-iframe selection + SSRF redirect guard; TitleMap with upstream-vs-live drift noted). **Season cap `_MAX_SHOW_SEASONS=10` kept** per #138 audit (measured live 2026-08-08): a cap-off `content()` for The Simpsons (37 seasons, fetched 6-wide) took 25.5s — inside the D6 >30s budget but with no headroom — and the 38-request sweep tripped CMS rate-limiting (HTTP 429 / connection drops) that silently dropped 14/37 seasons. Dropping the cap is therefore not merely slow but **lossy**; the cap value 10 = 11 upstream requests for a show page, ~8-10s to complete (single page ~0.95s; the CMS serialises concurrent fetches — 6 parallel = 4.69s wall, only ~1.2× faster than sequential) and below the rate-limit burst threshold. Cap-on `content()` measured 7.8s. The price is that seasons 1-27 vanish from the show's browsable rail; they stay reachable only directly via their own season slug (`s5`, `sezon-N`).
5. `animeon` — landed in `backend/cs_uk_api/providers/animeon.py` (24 tests, ready; JSON API + XOR-decoded iframe → ashdi.vip direct m3u8)

Group 4:

1. `hentaiukr` — in scope per spec (no hiding); small file, should be straightforward

## How to triage one provider

1. Open the Kotlin source from the table above. Look for `ExtractorApi`,
   `ExtractorLink`, and `M3u8Helper` calls — these tell us how the URL is
   resolved.
2. Search the source for `eval`, `Function(`, or `obfuscated` — those are
   the markers of "JS execution required".
3. Capture one player page and one listing page from the live site with
   `curl` + `lftp` and save under
   `backend/cs_uk_api/tests/fixtures/<id>/`. Do **not** invent HTML
   (spec ground rule 7).
4. Run `mpv <stream_url>` against the captured URL on Linux as the live
   gate. If it plays, mark `ready`.
