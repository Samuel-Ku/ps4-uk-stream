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
  execute JS (not portable without a JS engine).
- **Verdict**:
  - `ready` — landed in `backend/cs_uk_api/providers/` and passing tests
  - `portable` — code is straightforward to port (HTML + regex/iframe), not yet started
  - `partial` — mixed; some content types resolve fine, others need JS
  - `not portable` — JS engine required, out of scope for v2

## Table

| Provider id | Upstream plugin | Kotlin sources | Search | Player | JS dep | Verdict |
| ----------- | --------------- | -------------- | ------ | ------ | ------ | ------- |
| uakino | [UakinoProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/UakinoProvider) | `UakinoProvider.kt` (14.6 KB) | HTML | iframe → regex | mild | **ready** |
| uaflix | [UAFlixProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/UAFlixProvider) | `UAFlixProvider.kt` (14.9 KB) | HTML, has mainPage | iframe → regex | mild | **ready** |
| animeua | [AnimeUAProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/AnimeUAProvider) | `AnimeUAProvider.kt` (8.2 KB), `Tracker.kt` | HTML | iframe → JSON `file:` (dubs or m3u8) | mild | **ready** |
| kinovezha | [KinoVezhaProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/KinoVezhaProvider) | `KinoVezhaProvider.kt` (10.3 KB) | HTML | iframe → regex (torDecrypt) | mild | **ready** |
| banderakino | [BanderakinoProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/BanderakinoProvider) | `BanderakinoProvider.kt` (14.2 KB) | HTML | TBD | TBD | TBD |
| bambooua | [BambooUAProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/BambooUAProvider) | `BambooUAProvider.kt` (8.9 KB), `JSONModel.kt` | HTML + JSON | TBD | mild | TBD |
| coaninet | [CoaninetProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/CoaninetProvider) | `CoaninetProvider.kt` (12.3 KB) | JSON API | pre-resolved HLS master | none | **ready** |
| klontv | [KlonTVProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/KlonTVProvider) | `KlonTVProvider.kt` (10.0 KB), `Tracker.kt` | HTML | iframe → regex | mild | **ready** |
| uaserialspro | [UASerialsProProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/UASerialsProProvider) | `UASerialsProProvider.kt` (20.5 KB) | HTML | TBD | TBD | TBD |
| eneyida | [EneyidaProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/EneyidaProvider) | `EneyidaProvider.kt` (20.6 KB) | HTML | iframe → PlayerJS JSON | mild | **ready** |
| anitubeinua | [AnitubeinuaProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/AnitubeinuaProvider) | `AnitubeinuaProvider.kt` (23.4 KB) | HTML | TBD | TBD | TBD |
| animeon | [AnimeONProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/AnimeONProvider) | `AnimeONProvider.kt` (56.7 KB) | HTML | TBD | TBD | TBD (heavy file → likely packed JS) |
| kinotron | [KinoTronProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/KinoTronProvider) | `KinoTronProvider.kt` (7.9 KB) | HTML | iframe → inline JSON | mild | **ready** |
| hentaiukr | [HentaiUkrProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/HentaiUkrProvider) | `HentaiUkrProvider.kt` (5.3 KB) | JSON manifest + plur.cfg.json | mp4 (per-source highest-quality pick) | none | **ready** (in scope per spec; no hiding) |
| doramyworld | [DoramyWorldProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/DoramyWorldProvider) | `DoramyWorldProvider.kt` (8.3 KB), `JSONModel.kt` | HTML + JSON | TBD | mild | TBD |
| cikavaideya | [CikavaIdeyaProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/CikavaIdeyaProvider) | `CikavaIdeyaProvider.kt` (8.2 KB) | HTML | regex (ashdi.vip `file:`) | mild | **ready** |
| ufdub | [UFDubProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/UFDubProvider) | `UFDubProvider.kt` (6.1 KB) | HTML | iframe → regex | none | **ready** |
| unimay | [UnimayProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/UnimayProvider) | `UnimayProvider.kt` (6.6 KB) | JSON | hls.master URL | none | **ready** |
| serialno | [SerialnoProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/SerialnoProvider) | `SerialnoProvider.kt` (10.1 KB) | HTML | iframe → torDecrypt | mild | **ready** |
| simpsonsuatv | [SimpsonsUATvProvider](https://codeberg.org/CakesTwix/cloudstream-extensions-uk/src/branch/master/SimpsonsUATvProvider) | `SimpsonsUATvProvider.kt` (29.3 KB) | HTML | TBD | TBD | TBD |

## Per-provider "owner" field (suggested order)

Group 1 (simple, HTML-based, `<15 KB`):

1. `ufdub` — landed in `backend/cs_uk_api/providers/ufdub.py` (10 tests, ready)
2. `unimay` — landed in `backend/cs_uk_api/providers/unimay.py` (13 tests, ready; JSON API, not HTML)
3. `kinotron` — landed in `backend/cs_uk_api/providers/kinotron.py` (9 tests, ready)
4. `cikavaideya` — landed in `backend/cs_uk_api/providers/cikavaideya.py` (12 tests, ready)
5. `animeua` — landed in `backend/cs_uk_api/providers/animeua.py` (9 tests, ready; supports `translations_level="episode"`)
6. `uaflix` — landed in `backend/cs_uk_api/providers/uaflix.py` (21 tests, ready; uses shared `RegexExtractor`)
7. `kinovezha`, `bambooua`
8. `coaninet` — landed in `backend/cs_uk_api/providers/coaninet.py` (8 tests, ready; JSON API, single-hop stream)

Group 2 (medium, 10–25 KB):

1. `klontv` — landed in `backend/cs_uk_api/providers/klontv.py` (10 tests, ready; DLE + ashdi.vip iframe chain)
2. `serialno` — landed in `backend/cs_uk_api/providers/serialno.py` (12 tests, ready; homepage-as-listing, tortuga.tw iframe + torDecrypt — code-duplication with KinoVezha noted as follow-up)
3. `banderakino`, `doramyworld`

Group 3 (heavy, needs deeper triage):

1. `eneyida` — landed in `backend/cs_uk_api/providers/eneyida.py` (12 tests, ready; DLE + PlayerJS JSON season/episode playlists)
2. `uaserialspro`, `anitubeinua`, `simpsonsuatv`
3. `animeon` — read the source before committing; packed JS risk

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
