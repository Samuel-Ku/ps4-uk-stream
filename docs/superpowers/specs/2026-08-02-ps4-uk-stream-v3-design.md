# PS4 UK Stream — Design Spec (v3: 10-foot UI)

**Date:** 2026-08-02 (grilling session)
**Status:** Approved
**Base:** [v2 spec](2026-08-01-ps4-uk-stream-design.md) + user's 10-foot UI input document
**Author:** Grilling session with the user

> **Superseded (2026-08-09):** the client-side design in §2+ targeted the
> in-house PS4 catalog client, which was removed when the project moved
> fully to **Switchfin** (a Jellyfin client) against the backend's
> Jellyfin facade (spec #100). The backend/data-model sections remain
> current.

## Amendment (2026-08-02): Russian-content blocking (issue #79)

The backend already blocks Russian-origin content; this amendment documents
the shipped behaviour (it was previously implemented but untested and
unspecified):

- **Config flag:** `CS_UK_BLOCK_RUSSIAN` env var, **default on** (`"1"`).
  Setting it to anything else disables the block.
- **Country field:** `ContentResponse.country` (`str | None`) carries the
  content's country of origin as parsed from the provider page (see
  `cs_uk_api.country` for the blocklist and fail-open semantics: `None` is
  never blocked).
- **Blocked content:** when the flag is on and `country` is blocked,
  `GET /api/content/{id}` returns **404** (`not_found`).
- **Blocklist cache:** the blocked content id is recorded in an in-memory
  blocklist cache (TTL = `CS_UK_CACHE_CONTENT`, **30 min** default) so
  repeat requests short-circuit with 404 without re-fetching the provider.

## 1. Purpose and Scope

v3 adds the 10-foot UX layer ("Netflix-like" catalog for a PS4 gamepad-only
environment viewed from ~3 m) on top of the working v2 catalog. The v2 spec
remains the contract for everything not modified here.

### Milestone structure (hard gates)

| Gate | Content | Unblocks |
|------|---------|----------|
| **M0** — Console smoke | Install current PKG on FW 11.00 + GoldHEN, fill `docs/switchfin-test-report.md` checklist (launch, menu render via SDL2, black-hole backend test) | M1 |
| **M1** — PS4 playback path | Replace `mpv_stub` with a real playback path on the console (ffmpeg-based; the `ffmpeg-ps4.sh` toolchain exists) | M2 late, all player sections |
| **M2** — 10-foot restyle | Typography scale, safe margins, focus highlight, Home screen skeleton, chips | M3 |
| **M3** — Features | Merged groups in backend, resume state, source/dub memory, health dimming, Newest row | — |

Player-OSD, buffering, quality-switch and subtitle requirements from the input
document are **blocked-by-M1** and are not part of M2/M3 scope except as noted.

### Fact anchors (verified in code 2026-08-02)

- On-console test was never run: `docs/switchfin-test-report.md` is an unfilled template.
- No PS4 playback path exists: `src/player/ps4_stubs/mpv_stub.h` documents libmpv replacement as a follow-up; `gl_renderer_stub.cpp` is a no-op (UI renders via vendored SDL2).
- Current catalog typography: `kTitleSize 28 / kBodySize 18 / meta 16–18` (below the 24 px floor).
- Input abstraction: `c2d::Input::Key{Up/Down/Left/Right/Fire1/2/3/Start}`; physical DualShock binding lives in the SDL2 input platform layer (single point of change).
- Content identity: `provider:external_id`; no cross-provider entity resolution anywhere.
- No resume/history: v2 explicitly excluded recently-watched; `Player::resume()` is just unpause.
- Writable data path exists: `Io::getDataPath()` hosts `client.cfg` + `cache/` (the client data dir on PS4).
- Posters: backend memory TTL 1 h + client 50-entry in-memory LRU; `cache/<hash>-poster.jpg` disk pattern already exists from the old scrapper.
- OSD already auto-hides: `OSD_HIDE_TIME 4.0f` in `player_osd.cpp`.
- "Newest" signals in providers: default listing order of type sections is newest-first (DLE sites); explicit recency sections: simpsonsuatv `updates`, unimay `updates`, animeon `page`, animeua `page`, anitubeinua `page`. "Popular" exists in exactly ONE provider: animeon `popular`.
- uakino requires the headless-Chromium browser-session provider (Cloudflare Turnstile).

## 2. Architecture deltas (vs v2)

### 2.1 Backend additions

1. **`GET /api/home`** — aggregated Home feed (see §3.1).
2. **Merged groups** — `/api/search` and `/api/home` return *grouped* cards (§4).
3. **Provider health** — in-memory sliding window per provider (last 20 upstream
   operations): error_rate ≥ 0.8 with ≥ 5 samples → `down`; 0.4–0.8 → `degraded`.
   `/api/providers` gains `status` (+ `last_error_at`). `/api/home` skips `down`
   providers. No persistence: backend restart = clean slate, self-healing.
   uakino gets a deterministic startup marker when Chromium is missing.

No other v2 endpoints change shape except as noted in §3.

### 2.2 Client (in-house fork — superseded by Switchfin) additions

- New `ScreenHome` — the single catalog entry point ("Каталог UA" in the client's
  main menu; the separate "Пошук UA" menu entry is removed; search lives as a
  loupe element at the top of Home).
- `catalog_state.json` in `Io::getDataPath()` — resume + source/dub memory
  (JSON via vendored cJSON; atomic write tmp+rename). **No SQLite.**
- Restyle constants for catalog screens only (§5). the client chrome (main menu,
  filer, settings) is NOT restyled in M2 — temporary visual mismatch accepted,
  upstream-drift minimized.

## 3. API contract additions

### 3.1 `GET /api/home`

- Response: ordered list of rows, each `{ id, title, items[] }` where `items[]`
  share the grouped-card shape of `/api/search` results (§4.1).
- Rows (in order, conditional rows omitted when empty):
  1. `Новинки` — merge of default (page 1) listings of all healthy providers,
     round-robin interleaved, deduplicated by `groupKey`, ≤ 20 items. Ordering
     within = each site's own listing order (no date parsing in v3).
  2. `Популярні зараз` — only from providers exposing a native top/popular
     section (today: animeon). **Conditional**: absent when no such provider
     is healthy. No synthetic popularity.
  3. Type rows: `Фільми`, `Серіали`, `Аніме`, `Мультфільми`, `Дорами` —
     conditional on coverage by a healthy provider, ≤ 20 items each. The tail
     slot of a full row links to the corresponding browse screen ("Ще").
- Cache: 30 minutes, server-side.
- ("Продовжити перегляд" is NOT a backend row — the client prepends it from
  `catalog_state.json`; see §7.)

### 3.2 Grouped cards (search + home)

```json
{
  "group": "g1:3f9a...c1",
  "title": "Дюна",
  "year": 2021,
  "type": "movie",
  "poster": "/api/poster?u=...",
  "sources": [
    { "provider": "uakino", "id": "uakino:4567" },
    { "provider": "eneyida", "id": "eneyida:88" }
  ]
}
```

- `sources` comes from search/browse data only — no extra upstream calls at
  listing time (lazy sources, §4.3).

### 3.3 Group details (lazy)

- `GET /api/content/{groupKey}?source=<provider>` returns the v2 content
  response **of that one source** (unchanged shape), plus the group's
  `sources[]` echo. Fetching another source = a new request with a spinner.
- A dead source degrades the chip, not the screen.

### 3.4 `GET /api/providers`

- Gains: `status: "ok" | "degraded" | "down"`, `last_error_at: <iso|null>`.

## 4. Cross-provider merging

### 4.1 Normalization (`normalize_title`)

- lowercase; strip punctuation; unify apostrophes (`' ’ `` ` → `'`);
- extract `(YYYY)` into the year field; strip quality tags
  (720p/1080p/HDRip/...) and tail markers (`фільм`/`серіал`/`мультфільм`,
  `(фільм 2021)`-style suffixes).

### 4.2 Merge rule (strict + year-soft)

Two items merge **iff**:
normalized title equal **AND** `type` equal **AND** (`year` equal **OR** at
least one side has unknown year).
Consciously sacrificed: legit matches with conflicting years.

### 4.3 Group key and laziness

- `groupKey = "g1:" + sha1(norm_title | type | year)` — stateless, recomputed
  from listing data. The `"g1:"` prefix versions the algorithm: any change to
  normalization rules = bump to `"g2:"` (old client state entries expire via
  LRU; no migrations ever).
- Details of non-focused sources are never fetched eagerly (latency; a hung
  provider would otherwise block the screen).

### 4.4 Testing and audit

- Parametrized unit tables for `normalize_title` (real Ukrainian title pairs:
  apostrophe variants, `«Дюна (2021)»` vs `«Дюна»`, `«Тато / Daddy»`).
- Merge tests on **live-captured** fixtures (spec ground rule: no invented
  HTML): one query run against ≥ 3 providers, captures frozen, expected groups
  asserted.
- Every merge decision logged at INFO with both raw titles
  (`merge: 'Дюна (2021)[eneyida:88]' + 'Дюна[uakino:4567]'`) — the only
  after-the-fact wrong-merge detector.

## 5. UI spec (M2)

### 5.1 Typography and layout (catalog screens only)

- `kSmallSize = 24`, `kBodySize = 28`, `kTitleSize = 32` (px @1080p).
- Layout origin `(96, 54)` on 1080p (5% action-safe). Note: the input
  document's 27/48 px figure is actually 2.5% — rejected.
- Dark theme (the client's default); pure white backgrounds forbidden in new screens.
- Focus highlight constant: border + scale 1.05; navigation order strictly
  top-down / left-right predictable (no illogical focus jumps).

### 5.2 Home (`ScreenHome`)

- Top: search loupe (focusable, opens the existing on-screen keyboard screen).
- Rows per §3.1; "Продовжити перегляд" prepended client-side when non-empty.
- Backend unreachable on entry → error screen in place of Home:
  "Сервер недоступний" + hint ("Перевірте ПК і Налаштування → Адреса сервера")
  + "Повторити" (X). No background retries. Other client menus unaffected.
  (Covers the M0 black-hole checklist item.)

### 5.3 Details screen

- Poster + short description + **source chips row** (one chip per
  `sources[]`, grayed when the provider is `down`) + big default-focused
  "Дивитись" button.
- Seasons/episodes/translation selector belong to the **currently focused
  source** (current v2 behavior carries over: episode-level translation cycles
  inside the episode row with Triangle/Fire3).
- **Source switch = cursor reset to S1E1** of the new source; no position
  mapping across sources (episode structures are non-isomorphic).
- No separate dub-step screen (the input document's "step before the player"
  conflicts with its own ≤3-level limit and with source-memory pre-focus).

### 5.4 Search

- Provider filter chips only: «Усі» + one per provider (grayed when `down`).
  **No dub-language filter** (no listing-level data; near-zero discriminating
  power in an all-UA catalog).

### 5.5 Buttons (western scheme; semantics fixed here, codes land in M0/M1)

| Button | Action |
|--------|--------|
| Cross (X) | confirm / play |
| Circle (O) | back |
| Options | menu |
| Triangle | cycle episode translation |
| Square | filter/edit |
| L1/R1 | paging |

### 5.6 Navigation depth

Home → details → player (3 levels). Movie path: Home → details → player.
Search path: Home → keyboard → results → details → player. Source switching
happens inside details (does not add a level).

### 5.7 Back-stack

Leaving the player returns to the details screen with cursor, focused source
and scroll position intact (standard client behavior, kept).

## 6. Error UX

- Code → human string mapping (client-side, `.po`-style table):

  | code | string |
  |------|--------|
  | `upstream_unreachable` / 502 | «Джерело тимчасово недоступне» |
  | backend down / timeout | «Сервер недоступний» |
  | empty results | «Нічого не знайдено» |
  | `translation_missing` | «Цей переклад недоступний» |

- Non-modal toasts, ~3 s auto-dismiss. **No automatic retries anywhere**
  (v2 YAGNI kept) — not even one on `/api/stream`; retry = deliberate X press.

## 7. Local state (`catalog_state.json`)

- **Resume** (per `groupKey`): `{ source: {provider, id}, episode_id?,
  translation_label, position_sec, duration_sec, updated_at }`.
  - Write: on player exit **and** every 10 s during playback (mpv on Linux;
    PS4 after M1).
  - `position_sec >= 95% * duration_sec` → entry marked finished.
  - Home "Продовжити перегляд" row: ≤ 20 most recent groups; selecting an item
    opens the details screen pre-focused (source chip + episode cursor on the
    next unfinished episode). **Never auto-plays.**
- **Source/dub memory** (per `groupKey`, series only — movies are not
  remembered): `{ provider, translation_label }`. Applied as **pre-focus** on
  the details screen; "Дивитись" stays an explicit press. Dead remembered
  provider → gray chip, focus moves to the next healthy source.
- Cap: 50 entries LRU per store.
- PS4 resume persistence is M1-gated (player must report position).

## 8. Caching policy (delta)

- Posters: **disk cache 7 days on both sides** (backend; client reuses the
  `cache/<hash>.jpg` pattern). Posters treated as immutable.
- `/api/home`: 30 min (§3.1). Search/browse/content memory TTLs unchanged
  (5/30 min). No persistent content cache (stale episode lists risk).

## 9. Deferred / out of scope for v3

- Quality badges (720p/1080p) and manual quality selection: **deferred**.
  After M1, add *additive* `variants: [{label, url}]` to `/api/stream`
  populated **only** when the extractor natively sees variants (m3u8 master
  playlists, explicit quality lists). HEAD/manifest probing for badges: never.
- Subtitles: **full defer**, including schema. (Linux mpv supports `sub-add`,
  but the PS4 player does not exist yet; extractors don't expose tracks today.)
- Genre/year filters, accounts, watch-history sync — still out (v2).
- client chrome restyle (main menu, filer, settings, player OSD layout).
- OSD auto-hide — already satisfied (4 s), no change.

## 10. Conscious deviations from the input document

| Document asks | Decision | Why |
|---|---|---|
| Dub-language filter in search | Rejected | No listing-level data; no discriminating power |
| Quality badges per source | Deferred | No data without probing; blocked-by-M1 |
| Separate dub-selection step | Rejected | Conflicts with ≤3-level rule and memory pre-focus |
| SQLite for resume | Rejected → JSON file | `Io::getDataPath()` + vendored cJSON suffice; 50-entry LRU |
| 5% margin = 27/48 px | Corrected to 96×54 px | Their figure is 2.5%; classic action-safe is 5% |
| Merged cross-provider dubs on one title | **Accepted, full backend merge** | With strict+year-soft rule, lazy sources, audit log |
| Subtitles (mpv can already) | Deferred | True only on Linux; PS4 player = M1 |

## 11. Resolved decisions (grilling, 2026-08-02)

1. Document accepted as **v3 vision in 4 milestones** (M0 console smoke → M1
   playback path → M2 restyle → M3 features); player sections blocked-by-M1.
2. Home is the single catalog entry point; new `/api/home` endpoint;
   provider-first browsing survives as "Джерела" screens.
3. **Full cross-provider merging in the backend.**
4. Merge rule: normalized title exact + same type + (year equal or one
   unknown). Fuzzy matching rejected.
5. Lazy sources: groups carry `sources[]` from listings; details fetched only
   for the focused source; `groupKey = "g1:" + sha1(title|type|year)`;
   Home dedupes by the same key.
6. Source/dub memory: per-groupKey, **pre-focus, manual start**; movies not
   remembered; dead source = gray chip.
7. Resume: JSON state file, per-groupKey, saved on exit + every 10 s,
   ≥ 95% = finished, row ≤ 20, row → details pre-focus, no autoplay.
8. Provider health: backend in-memory sliding window (20 samples; 0.8 → down,
   0.4 → degraded); exposed in `/api/providers`; Home skips `down`.
9. Search filters = provider chips only.
10. Source/translation chips live **on the details screen**; big "Дивитись"
    default-focused; no interstitial step.
11. Quality badges/variants deferred; `variants[]` additive after M1 from
    native extractor data only.
12. Subtitles: full defer, schema untouched.
13. Western button scheme fixed as semantics; physical codes in M0/M1.
14. Restyle limited to catalog screens: 24/28/32 px, origin (96, 54), dark
    theme, no pure white; the client chrome untouched.
15. Posters → 7-day disk cache on both sides; other TTLs unchanged.
16. Home rows: resume → **Новинки** (merged round-robin, groupKey-deduped,
    ≤ 20) → **Популярні зараз** (conditional, native top-sections only) →
    conditional type rows (Фільми → Серіали → Аніме → Мультфільми → Дорами,
    ≤ 20, "Ще" tail to browse).
17. Human error strings, non-modal toasts, **no automatic retries**.
18. Source switch inside a group resets the episode cursor to S1E1; no
    cross-source position mapping.
19. Merge testing: parametrized normalize tables + live-captured multi-provider
    fixtures + INFO merge-audit log.
20. Backend-down Home = error screen with "Повторити"; no background retries.
21. client main menu: single "Каталог UA" entry; search = loupe atop Home.
22. `groupKey` algorithm versioned by the `"g1:"` prefix; bump on rule
    changes, never migrate.
