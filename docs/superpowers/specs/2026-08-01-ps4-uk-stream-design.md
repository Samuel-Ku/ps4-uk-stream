# PS4 UK Stream — Design Spec (v2)

**Date:** 2026-08-01 (rewritten after grilling session)
**Status:** Approved
**Author:** Grilling session with the user

> **Superseded (2026-08-09):** this spec designed the original in-house
> PS4 catalog client ("the client" below). The project moved fully to
> **Switchfin**, a Jellyfin client, against the backend's Jellyfin facade
> (spec #100); the current architecture is in
> [2026-08-05-jellyfin-adapter.md](2026-08-05-jellyfin-adapter.md). This
> document is retained as the historical record of the original approach.

## 1. Purpose and Scope

A PS4 homebrew application, built on an in-house client that streams Ukrainian-dubbed content (films, series, anime, cartoons, doramas) sourced from the providers of [cloudstream-extensions-uk](https://github.com/CakesTwix/cloudstream-extensions-uk). The Android/Kotlin layer is replaced with a Linux-side HTTP service that the PS4 client queries over the local network.

### Target environment
- **Console:** PS4 firmware 11.00 with GoldHEN.
- **Backend host:** a Linux PC or laptop running the scraper service on a known local IP.
- **Network reachability:** the PS4 connects only to the local network; remote access to the host (e.g. Tailscale) is out of scope for the console.

### In scope (full catalog)
- **All 20 content providers** from the upstream repository (21 minus SyncPlugin, which is not a content source — it is a watch-history sync plugin):
  Uakino, UAFlix, AnimeUA, KinoVezha, Banderakino, BambooUA, Coaninet, KlonTV, UASerialsPro, Eneyida, Anitubeinua, AnimeON, KinoTron, HentaiUkr, DoramyWorld, CikavaIdeya, UFDub, Unimay, Serialno, SimpsonsUATv.
- **Cloudstream-like catalog UX**, not just search:
  - Section browsing per provider (e.g. "Нові фільми", "Серіали", "Аніме", "Дорами", "Мультфільми") with pagination.
  - Title search via on-screen keyboard as the second entry point.
- Detail view with poster, description, translation/voice-over selection, and season/episode lists for series.
- **Per-episode translations** (anime sources attach dub/sub choices to individual episodes).
- Direct playback through the client's existing MPV layer.
- **HentaiUkr is in scope, in the last implementation group, without any hiding/disabled-by-default flag** (user decision).

### Out of scope
- Genre/year filters, accounts, watch-history sync (SyncPlugin), subtitles, bookmarks, recently-watched.
- Other client platforms (Switch, Vita, Linux desktop) in the context of this project.

### Feasibility threshold (JS-free)
- Stream resolution must be reimplementable in Python without a full JavaScript engine.
- A provider whose player requires executing obfuscated JS is classified `not portable` in the triage table and is **not** counted as "ready". This may exclude 1–3 sources of the 20; the rest must work for real.

## 2. Architecture

Two cleanly separated components communicating over HTTP+JSON.

### 2.1 Backend (Python, local server)
- `cs_uk_api/` — a FastAPI application.
  - `main.py` — entry point and routes.
  - `providers/` — one adapter per upstream provider; all implement the shared `BaseProvider` (`search`, `content`, `stream`, `main`).
  - `extractors/` — the stream-resolution layer shared by all providers:
    - `base.py` — `BaseExtractor` and result type.
    - `iframe.py` — follow iframe chains, return direct URL.
    - `playerjson.py` — port of the Cloudstream "PlayerJson" pattern (CDN player returns JSON; pull `.mp4`/`.m3u8` + headers).
    - `regex.py` — generic `file:` / `source:` regex extraction (Uakino-style).
    - Custom per-provider extractors live next to their adapter.
  - `models/` — Pydantic DTOs for the API.
  - `services/` — shared HTTP client (`httpx`), TTL cache, poster proxy, logging.
  - `config.py` — bind address, port, timeouts, list of active providers.
- **Dependencies:** `httpx`, `beautifulsoup4`, `lxml`, `fastapi`, `uvicorn`, `pydantic`, `cachetools`.
- **Run command:** `uvicorn cs_uk_api.main:app --host 0.0.0.0 --port 8000`; optional `systemd` unit for persistence.

### 2.2 In-house client (C++)
- New isolated module `client/src/catalog/`, no changes to the MPV core.
  - `CatalogApi.{h,cpp}` — the only HTTP surface of the client. Owns one `Browser` instance (libcurl is already there) and **one dedicated worker thread with a request queue** — the `Browser` is synchronous and not thread-safe (single CURL handle, single response buffer), so all requests are serialized.
  - `Json.{h,cpp}` — minimal cJSON wrapper (vendored `external/cJSON`).
  - `OnscreenKeyboard.{h,cpp}` — reusable focus-grid widget with correct UTF-8 handling (Cyrillic letters are multi-byte; appending must decode the label codepoint, not take `label[0]`).
  - `ScreenSections.{h,cpp}` — provider + section browser (new, drives `/api/sections` + `/api/browse`).
  - `ScreenSearch.{h,cpp}` — query screen with the on-screen keyboard.
  - `ScreenResults.{h,cpp}` — result list with posters (lazy poster loading via `CatalogApi::loadPoster`).
  - `ScreenContent.{h,cpp}` — detail view, season/episode strip, translation chooser (content-level or per-episode).
- `client/src/main.cpp` + `client/src/menus/menu_main.cpp` — add a "Каталог UA" item to the main menu (`Main::MenuType::Catalog`), following the existing `main->show(MenuType::...)` pattern.
- `client/data/common/client.cfg` + `client_config.{h,cpp}` — new option `OPT_CATALOG_URL` (default `http://192.168.2.223:8000` — LAN IP of the host, verified).
- Build: reuse the existing `ffmpeg.sh` pipeline; add `cJSON` as a submodule.

### 2.3 Data flow

```
PS4 (in-house client)                           Linux host
+----------------+    HTTP+JSON         +----------------------+
| Sections screen| ---> /api/sections   | FastAPI              |
| Browse screen  | ---> /api/browse     |  ├ 20 provider adapters
| Keyboard screen| ---> /api/search?q=  |  ├ extractors/ layer
| Results screen | ---> /api/content/   |  └ ...               |
| Detail screen  | ---> /api/stream/    +----------------------+
| MPV player     | <--- /api/poster/    ~      ~
+----------------+    GET URL           ~ CDN (mp4/m3u8)
```

### 2.4 Why this split
the client stays a clean media player; the fragile scraping logic lives on the host and is updated without rebuilding the PKG. Each provider adapter is isolated, so a single broken site does not break the rest of the catalog. The extractor layer is shared so 20 adapters do not mean 20 bespoke stream resolvers.

## 3. API Contract

### 3.1 General rules
- UTF-8, `application/json` responses.
- Errors as `{ "error": "<short_code>", "message": "<human phrase>" }` with the appropriate 4xx/5xx status.
- All requests from PS4 add header `X-Client: in-house-client/1.0` (enables future rate limiting).
- Single upstream call timeout: 8 s. Total `/api/search` budget: 12 s, fanned out to all active providers in parallel. Same 12 s budget for `/api/browse`.

### 3.2 Types

The catalog domain is governed by [ADR-0001: Catalog taxonomy: form + style](../../adr/0001-catalog-taxonomy-form-and-style.md). Authoritative definitions live in [CONTEXT.md](../../../CONTEXT.md).

```text
MediaForm        = "movie" | "series"                                       # required on every item
MediaStyle       = "anime" | "cartoon" | "dorama"                           # optional, frozenset on item
StreamType       = "mp4" | "m3u8" | "hls" | "dash"
TranslationLevel = "content" | "episode"
```

The single `MediaType` literal from the original v2 spec is obsolete: form and style are independent axes, so ambiguous content like "дитяче аніме мультики" can carry both `form: "series"` and `styles: {"anime", "cartoon"}`. Examples in §3.3–§3.6 still illustrate the legacy `type` field for readability — in the actual contract `type` is replaced by `form` + `styles`.

### 3.3 `GET /api/sections`
- 200 response:
  ```json
  [
    {
      "provider": "uakino",
      "name": "Uakino",
      "sections": [
        { "id": "filmy", "title": "Фільми", "type": "movie" },
        { "id": "animeukr", "title": "Аніме", "type": "anime" }
      ]
    }
  ]
  ```
- Providers without section browsing return `sections: []` and are omitted.

### 3.4 `GET /api/browse?provider=...&section=...&page=N`
- 200 response:
  ```json
  {
    "provider": "uakino",
    "section": "filmy",
    "page": 1,
    "has_next": true,
    "results": [
      {
        "id": "uakino:4567",
        "provider": "uakino",
        "type": "movie",
        "title": "Дюна",
        "year": 2021,
        "poster": "/api/poster?u=https%3A%2F%2F...%2Fthumb.jpg",
        "url": "https://uakino.best/film/..."
      }
    ]
  }
  ```
- 400 if the provider/section is unknown; 502 if the upstream is unreachable.

### 3.5 `GET /api/search?q=...&provider=...`
- Same as before; `provider` optional (`all` default).
- 400 if `q` is empty or longer than 80 chars.

### 3.6 `GET /api/content/{id}`
- `id` format: `<provider>:<external_id>`.
- Content-level translations (movies, most series):
  ```json
  {
    "id": "uakino:4567",
    "type": "movie",
    "title": "Дюна",
    "year": 2021,
    "description": "...",
    "poster": "/api/poster?u=...",
    "translations_level": "content",
    "translations": [{ "id": "uk", "label": "Українська" }],
    "seasons": null
  }
  ```
- Episode-level translations (anime): `translations_level: "episode"`, content-level `translations` empty, and each episode carries its own:
  ```json
  {
    "id": "animeua:123",
    "type": "anime",
    "title": "...",
    "translations_level": "episode",
    "translations": [],
    "seasons": [
      { "number": 1, "episodes": [
        { "number": 1, "id": "animeua:123:s1e1", "title": "Серія 1",
          "translations": [{ "id": "dub", "label": "Український дубляж" },
                           { "id": "sub", "label": "Українські субтитри" }] }
      ]}
    ]
  }
  ```
- 404 if the id is unknown.

### 3.7 `GET /api/stream/{contentId}?translation=...`
- `contentId` is the content id for a movie, or the episode id for a series. `translation` is the translation id from the appropriate level (content or episode).
- 200 response:
  ```json
  {
    "url": "https://cdn.example.com/.../movie.mp4",
    "type": "mp4",
    "headers": { "Referer": "https://...", "User-Agent": "..." }
  }
  ```
- 502 with `error: "upstream_unreachable"` if the source is temporarily unavailable; 404 `error: "translation_missing"` if the requested translation does not exist for that episode.

### 3.8 `GET /api/poster?u=...`
- Proxy: `httpx GET` on `u`, returns `image/jpeg` or `image/png`.
- 5 s timeout, 4 MB size cap, 1 hour cache, http/https only.

### 3.9 `GET /api/providers`
- List of active providers:
  ```json
  [{ "id": "uakino", "name": "Uakino", "types": ["movie","series","anime"] }]
  ```

## 4. UI Flow and Module Structure

### 4.1 Navigation stack
```
Main Menu
└─ Каталог UA (new)
   ├─ ScreenSections          ← providers → sections (L1/R1 page)
   │    └─ ScreenResults      ← browse results
   │         └─ ScreenContent
   └─ ScreenSearch            ← on-screen keyboard
        └─ ScreenResults      ← search results
             └─ ScreenContent
                  └─ [season] → [episode] → [translation if episode-level]
                       └─ Existing Player (MPV)
```

### 4.2 `ScreenSections`
- Inherits `c2d::Scene`. Left column: providers (from `/api/sections`); right column: that provider's sections. Cross/Down enters a section.
- Calls `CatalogApi::browse(provider, section, page)` on the worker thread; spinner while loading.
- Back (Square) returns to main menu.

### 4.3 `ScreenSearch`
- As before: title "Пошук", input field, on-screen keyboard, spinner, error toast "Помилка мережі. Спробувати ще?".
- Remote mapping: arrows — move focus; Cross/Enter — press a key; Triangle — backspace; Start/Options — search; Square — back.

### 4.4 `ScreenResults`
- Vertical list: 200×280 poster + title + year + type badge (Фільм/Серіал/Аніме/Мультфільм/Дорама).
- Posters loaded lazily via `CatalogApi::loadPoster` (must be actually implemented — it was missing from the previous plan).
- Paginated 20 at a time, R1/L1 to page (browse mode only).
- Empty state: "Нічого не знайдено" + "Назад".

### 4.5 `ScreenContent`
- Left: large poster. Right: description + translation list.
- Series: season strip (horizontal) + episode list (vertical).
- If `translations_level == "episode"`, the translation chooser is bound to the focused episode, not to the content.
- Confirm → `CatalogApi::stream(id, translation)` → hand `url` + `headers` to the existing `Player::load()`.

### 4.6 `OnscreenKeyboard`
- Reusable widget, 5 rows × 10 columns, bottom row `space  back  clear  done`.
- **UTF-8 correctness requirement:** labels are multi-byte (Cyrillic, Ґ/Є/І/Ї). Pressing a key must decode the full UTF-8 codepoint of the label and append it — never `label[0]` (the previous plan appended only ASCII and made Cyrillic keys dead).

### 4.7 `CatalogApi`
- Owns: base URL, one `Browser` instance, one worker thread, a request queue, and result callbacks.
- All calls (`searchAsync`, `contentAsync`, `streamAsync`, `browseAsync`, `sectionsAsync`, `loadPoster`) return through callbacks; the UI thread never blocks.
- HTTP is serialized on the worker thread because `Browser` is synchronous and shares one CURL handle (verified in `src/filer/Browser/Browser.hpp`: `open(url, timeout)`, `open(url, post_data, timeout)`, `response()`, `getError()`, `status()`, `addheaders(...)`, `set_handle_redirect(...)`).
- JSON via `Json.{h,cpp}` (cJSON).
- Poster bytes are cached in memory (LRU, ~50 entries).

### 4.8 Integration points in existing code (verified)
- `src/main.cpp` lines ~107–115: `std::vector<MenuItem> items; items.emplace_back("Home", ...)` — add `items.emplace_back("Каталог UA", "catalog.png", MenuItem::Position::Top);`.
- `src/menus/menu_main.cpp` `MenuMain::onOptionSelection` — add `else if (item->name == "Каталог UA") { setVisibility(Hidden); main->show(Main::MenuType::Catalog); }`.
- `src/main.h` — add `Catalog` to `enum class MenuType`.
- `src/main.cpp` `Main::show(...)` — branch creating the catalog screens.
- `src/client_config.h` — `#define OPT_CATALOG_URL "CATALOG_URL"` (string macros, verified).
- `src/client_config.cpp` — `addOption({OPT_CATALOG_URL, "http://192.168.2.223:8000"});` after the `OPT_NETWORK` line (verified; host LAN IP from `enp1s0`).
- MPV layer (`Mpv`, `Player`) — untouched.

## 5. Error Handling

### 5.1 Backend
- All unhandled exceptions inside an adapter are caught and surfaced as `ProviderError` with one of: `not_found`, `unreachable`, `parse_failed`, `rate_limited`, `translation_missing`, `not_portable`.
- A failing provider does not break the rest: its result is omitted from `/api/search` and a `WARNING provider=<id> error=<...>` line is logged.
- A global middleware logs `method`, `path`, `provider`, `latency_ms`, `status`.
- `GET /api/stats` (LAN-only) exposes request counts, per-provider errors, average latency.

### 5.2 Client (in-house)
- `CatalogApi` distinguishes `error_network`, `error_http_<code>`, `error_parse` (same as before).
- All actions appended to `client.log` via the existing `c2d::Utility::Debug`.

### 5.3 Explicitly NOT in scope (YAGNI)
- Automatic client-side retries; offline mode; startup "backend unreachable" notification beyond a one-time toast.

## 6. Provider Coverage and Families

The 20 content providers, provisionally classified by the structure of their upstream Kotlin sources (verified file lists from the repo). The **triage task** in the plan re-verifies each against the live site and fixes misclassifications; the family determines which shared extractor the adapter uses.

| # | Provider | Type(s) | Family (provisional) |
|---|----------|---------|----------------------|
| 1 | Uakino | movie/series/anime/cartoon/dorama | reference adapter (regex `file:` + ajax playlists) |
| 2 | Serialno | series | simple-iframe |
| 3 | SimpsonsUATv | series | simple-iframe |
| 4 | UFDub | movie/series | simple-iframe |
| 5 | CikavaIdeya | movie/series | simple-iframe |
| 6 | AnimeUA | anime | playerjson |
| 7 | Banderakino | series | playerjson |
| 8 | Eneyida | movie/series | playerjson |
| 9 | KinoTron | movie/series | playerjson |
| 10 | KinoVezha | movie/series | playerjson |
| 11 | KlonTV | series | playerjson |
| 12 | BambooUA | anime | playerjson |
| 13 | DoramyWorld | dorama | playerjson |
| 14 | UASerialsPro | series | playerjson |
| 15 | Unimay | anime | playerjson |
| 16 | Anitubeinua | anime | custom extractors (Ashdi/Moon/csst) |
| 17 | Coaninet | anime | custom (JSON API client) |
| 18 | AnimeON | anime | custom |
| 19 | HentaiUkr | adult | custom (last group) |
| 20 | UAFlix | movie/series | custom / playerjson (triage) |

- SyncPlugin (watch-history sync) is **not a provider** and is excluded.
- The definitive `PROVIDERS.md` table in `backend/` carries the per-provider status: `✅ ready (plays in MPV)` / `⛔ not portable (JS)` / `⚠️ broken upstream`.

## 7. Testing Strategy

### 7.1 Backend (highest coverage, ≥80%)
- **Capture-first fixtures:** every adapter task begins by capturing real upstream HTML/JSON into `tests/fixtures/<provider>/` (sanitized of personal data), then writes tests against those frozen bytes. **No invented HTML** (the previous plan's Uakino fixtures were fictional and are discarded).
- **Unit (pytest):** one test per adapter per method (`main`, `search`, `content`, `stream`).
- **Extractor tests:** unit tests per extractor with frozen player pages.
- **Contract tests:** FastAPI `TestClient` validates every response against its Pydantic schema.
- **Integration (opt-in, `--integration`):** real requests, rate-limited to 1 req/10 s.
- **Live gate (per provider, required for `ready`):** `scripts/gate.sh <provider>` runs search → content → stream and pipes the resolved URL into `mpv --no-video` on Linux; PASS only if mpv reaches playback.

### 7.2 In-house client (native build, without PS4)
- Unit tests for `Json`, `OnscreenKeyboard` (UTF-8), `CatalogApi` parsing (mocked HTTP).
- `CatalogApi` HTTP layer tested on Linux against the real backend (integration).
- PS4 PKG build via Docker + OpenOrbis; `readoelf` + PKG magic validation.

### 7.3 On the PS4 (Definition of Done)
- Manual checklist: launch → "Каталог UA" → sections browse with posters → search → movie plays → series season/episode → anime episode-level translation → several episodes in a row.
- Recorded in `docs/switchfin-test-report.md` with date, firmware, GoldHEN version.

### 7.4 Tooling
- Backend: `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy`.
- The client: Docker with OpenOrbis, `readoelf`/`PkgTool.Core`, `mpv` for the live gate.

## 8. Implementation Phases

- **Phase 0 — Setup (no code):** pin the client base, OpenOrbis v0.5.2, cJSON 1.7.x. Monorepo `ps4-uk-stream/`.
- **Phase 1 — Backend skeleton:** models (v2 contract), cache, client, `BaseProvider`, registry.
- **Phase 2 — Extractor layer:** `base` / `iframe` / `playerjson` / `regex` extractors with tests.
- **Phase 3 — Uakino reference adapter** against the real site (POST search, real selectors, capture-first fixtures).
- **Phase 4 — FastAPI routes:** sections + browse added to the contract.
- **Phase 5 — Triage:** classify all remaining 19 providers, produce `PROVIDERS.md`.
- **Phase 6 — Provider groups:** Group 1 (simple-iframe) → Group 2 (playerjson) → Group 3 (custom) → Group 4 (HentaiUkr last). One task per provider, each with capture-first fixtures and the live gate.
- **Phase 7 — in-house client core:** `Json`, `CatalogApi` (honest Browser/worker-thread integration), `OnscreenKeyboard` (UTF-8), screens, menu entry, config.
- **Phase 8 — PS4 PKG build:** Docker + OpenOrbis, artifact validation.
- **Phase 9 — On-console test (FW 11.00 + GoldHEN):** manual checklist + report.

## 9. Configuration Defaults

- `OPT_CATALOG_URL` in `client.cfg` defaults to `http://192.168.2.223:8000`; editable from the PS4 settings menu ("Адреса сервера") without rebuilding the PKG.
- `CS_UK_PROVIDERS` env var (comma-separated) controls active providers; empty means all.
- HentaiUkr is enabled by default (user decision; no hiding flag).

## 10. Caching Policy

- Search responses: 5 minutes.
- Browse responses: 5 minutes.
- Content responses: 30 minutes.
- Posters: 1 hour (backend) + 50-entry LRU on the client.
- Justification: reduce scraping pressure on source sites (to avoid bans) while keeping catalog freshness.

## 11. Definition of Done (project level)

1. Backend runs on the Linux host; `/api/sections` lists providers; every provider marked `✅` in `PROVIDERS.md` passes the live gate (search → content → stream → plays in MPV).
2. The client builds as a Linux binary; catalog screens work against the live backend.
3. `PPLA00001.pkg` builds via Docker/OpenOrbis and installs on PS4 FW 11.00 + GoldHEN.
4. Manual checklist passes on the console for: sections, search, posters, movie playback, series season/episode playback, anime episode-level translation playback.
5. `docs/switchfin-test-report.md` written with PASS verdict.

## 12. Resolved Decisions (from grilling)

1. Scope is all 20 content providers + Cloudstream-like catalog (sections + search).
2. Extractor layer is mandatory; JS-free is the threshold; `not portable` providers are excluded from the "ready" count.
3. Section browsing is in scope (`/api/sections`, `/api/browse`, `ScreenSections`).
4. Anime per-episode translations are in scope (`translations_level`).
5. Provider order: by extractor family (simple-iframe → playerjson → custom → HentaiUkr), each with the MPV live gate.
6. HentaiUkr in scope, last, no hiding.
7. SyncPlugin removed from the provider list (not a content source).
8. Uakino adapter must be rewritten against the real site (POST `/ua/` search, `div.movie-item.short-item`, `file:` regex / ajax playlists); old fictional fixtures discarded.
9. Media types extended: movie/series/anime/cartoon/dorama.
10. Client side: Cyrillic keyboard bug fixed; poster loading actually implemented; `Browser` integration made honest (single worker thread, no invented statics).
