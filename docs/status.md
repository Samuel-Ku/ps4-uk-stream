# Implementation status

This document tracks what was delivered by the implementation pass that
followed the plan at
[`docs/superpowers/plans/2026-08-01-ps4-uk-stream-impl.md`](superpowers/plans/2026-08-01-ps4-uk-stream-impl.md).

## Delivered

### Backend (FastAPI, Python) -- 1020 tests passing (2026-08-14)

Release gate (2026-08-14, v1.0.0): `pytest` 1020 passed; `ruff check
cs_uk_api` clean; `mypy cs_uk_api` strict-clean on the shipped package
(test files excluded via `pyproject.toml` `exclude`).

Diagnostics + fix pass (2026-08-08, see `docs/diagnostics-2026-08-08.md`
and GitHub issues #112–#125): live-gate review of all 19 providers
found and fixed four code bugs (kinotron series episodes 404,
serialno Tortuga payload drift, ufdub series without episode lists,
animeon movies unplayable) plus five upstream drifts (coaninet API
moved to `api.coani.net`, klontv → `klonua.com`, anitubeinua playlist
layout, simpsonsuatv season-fetch bound, gate.sh `groups` contract).
Ufdub and uaflix now follow redirects via the `safe_get` allowlist;
boundary validation is `fullmatch` everywhere; uakino movies without a
`data-voice` fall back to a default translation instead of 500.

- `backend/cs_uk_api/` -- complete package.
- v1 endpoints: `GET /api/search`, `GET /api/content/{id}`, `GET /api/stream/{id}`,
  `GET /api/poster`, `GET /api/providers`, plus global logging middleware and
  error handler.
- v2 endpoints (added for issue #17): `GET /api/sections`, `GET /api/browse`.
- Pydantic models match the API contract in
  [`superpowers/specs/2026-08-01-ps4-uk-stream-design.md`](superpowers/specs/2026-08-01-ps4-uk-stream-design.md).
  Includes `TranslationLevel` ("content" | "episode") for per-episode dub
  selection (issue #9).
- TTL cache (5m search / 30m content / 1h posters) with 12s total budget
  for `/api/search` across all providers.
- Shared extractors layer (`providers/extractors.py`) for the
  iframe / PlayerJson / regex pipeline used by v2 stream resolution.
- **19 of 20 v2 providers landed** in `backend/cs_uk_api/providers/`
  (issue #17). One skipped — `banderakino`, live site offline (HTTP 522).
  Uakino — the sole JS-engine provider — landed via its headless-Chromium
  session (issues #193/#195): warmed in the background at startup,
  `warming` while cold. The registered 19:
  - `uakino`, `ufdub`, `unimay`, `kinotron`, `cikavaideya`, `hentaiukr`,
    `bambooua`, `kinovezha`, `animeua`, `uaflix`, `coaninet`, `eneyida`,
    `klontv`, `serialno`, `doramyworld`, `uaserialspro`, `anitubeinua`,
    `simpsonsuatv`, `animeon`
  - Each has its own `test_<id>.py` with 9–24 tests using `respx`-mocked
    live-captured fixtures (no invented HTML). All providers apply
    `re.fullmatch` slug validation at `content()` and `stream()`
    boundaries; shared `safe_get` helper in `http_client.py` enforces
    redirect host allowlists (SSRF defense-in-depth).
  - Shared helpers: `extractors/regex.py` (file:/sources:/iframe regex),
    `_tortuga.py` (Tortuga XOR-base64 decode, used by serialno +
    kinovezha + uaserialspro), `_crypto_uaserialspro.py` (AES-256-CBC +
    PBKDF2-HMAC-SHA512 player-config decrypt, requires `pycryptodome` dep).
  - See [`docs/provider-triage.md`](provider-triage.md) for the full
    per-provider status table.
- Live gate tooling (`backend/cs_uk_api/scripts/gate.sh <provider>
  [query]` / `gate.sh --all`) drives search → content → stream → mpv
  playback against the real site for smoke-testing (issue #30, spec
  §7.1); `backend/cs_uk_api/scripts/README.md` documents it.
- Switchfin manual-test pipeline (`scripts/switchfin_test.py`, issues
  #143–#148): cold-starts the uvicorn backend, tails its request-log
  middleware line (`METHOD path -> status (ms)`) as the detection
  channel, and verifies the wire. The 2 handshake steps (login + views)
  are self-issued headlessly; with a phone attached it drives the real
  Switchfin client via hold-tap adb input (see
  `docs/test-artifacts/switchfin/device-driving.md` — plain `input tap`
  is too fast for this Qt/SDL client and is missed between frame polls)
  through all 7 library
  views (open + first card + type-aware play), applies a per-step logcat
  error filter, and writes `docs/switchfin-test-report.md`. For each ❌
  step it also dumps `logcat-<step>.txt` (spec-required) and
  `backend-<step>.txt` (a deliberate extra channel, kept for triage —
  #150; gitignored like the logcat snapshots). Step definitions are data
  in `docs/test-artifacts/switchfin/steps.yaml` +
  `tap-coords.yaml` (populated by `--calibrate`). Run with
  `python scripts/switchfin_test.py`: it cold-starts the backend with
  `CS_UK_JF_CAPTURE_DIR` capture enabled, slices the run's real-client
  records into `backend/cs_uk_api/tests/fixtures/jellyfin/
  capture.real-client.jsonl` (never `capture.jsonl`), and its runner unit
  tests live in `backend/cs_uk_api/tests/test_switchfin_runner.py`.
  Issue #148 resolved the series-play endpoints against the Switchfin
  client source (branch dev): the real client emits
  `/Shows/{series}/Seasons` + `/Shows/{series}/Episodes` (its
  `apiShowSeasons`/`apiShowEpisodes` constants in
  `app/include/api/jellyfin/media.hpp`, called from `app/src/tab/
  media_series.cpp`) — the spec's `/Items?parentId={season}` is the
  JS-SDK spelling. The shipped `/Shows/…` patterns are therefore left
  unchanged; on-device confirmation is still pending (no device attached).
- Live smoke test confirmed `/api/providers` returns all registered
  providers and the validation/404 paths behave correctly.
- **Resume / Continue watching now persists (v1.1 increment, spec
  #247, tickets #248–#250, 2026-08-14).** Playback positions survive a
  backend restart from a single versioned JSON file (default
  `~/.cache/cs-uk-api/playback.json`, knob `CS_UK_RESUME_PATH`), items
  watched to ≥95% of their runtime leave the shelves, the store is
  capped at 50 entries with a ≤20-item row, and the resume/NextUp DTOs
  carry `RunTimeTicks` so the bar renders proportionally. The store is
  the first persisted domain object — ADR-0003's version-token rule
  applies (see the ADR note).
- **Personalized home rows (spec #252, 2026-08-14).** «Рекомендовано
  для тебе» (≤20) and «Схоже на X» (≤10) rank the home snapshot by a
  pure weighted-cosine content similarity over genre/people/year/form/
  style profiles (background warm, bounded concurrency, piggybacks the
  content cache), fed by the ≤3 most recent watched items and ≤50
  recent search queries (persisted beside the playback state, resume
  file schema v2). Watched items are excluded, signal-less rows are
  omitted, and each row is a plain home-row kind — the facade serves
  them through the existing view mechanism with zero client changes.
  `docs/status.md` test counts: 1072 passing (was 1020 at v1.0.0).
- **Favorites + played + UserData (spec #257, 2026-08-14).** The heart
  and the context-menu "mark played/unplayed" now work end-to-end:
  `POST/DELETE /Users/{uid}/FavoriteItems/{id}` and
  `/PlayedItems/{id}` answer the `UserDataResult` the client reads
  back, card/detail/episode DTOs carry `UserData` (favorite badge,
  played checkmark, progress bar), and the marks persist in a separate
  versioned `user-state.json` (knob `CS_UK_USER_STATE_PATH`, atomic
  writes, corrupt file → empty). The Remote and Live TV tabs answer
  graceful empties instead of 404s. Test counts: 1096 passing (was
  1090 at ship; +6 from the Gap T1–T4 verification passes #258–#261).
- **Home composition (spec #263, 2026-08-14).** «Новинки» retired in
  favour of a Netflix-style home: two form-split recent rows
  («Нещодавно додані: Фільми» / «: Серіали» — newest listings
  filtered by form, round-robin deduped, topped up from the
  form-section items under the cap) and up to six genre rails (top
  genres by profile-store coverage, Ukrainian labels, `genre:<slug>`
  view kinds). Row kinds resolve through one uuid5 view-id formula, so
  the new views ride the existing facade mechanism; the snapshot-only
  kinds re-resolve against a fresh home load when the cached snapshot
  is mid-invalidation. The runner sweep now drives `recent_movie` /
  `recent_series` (tap-coords re-calibration pending on-device). Test
  counts: 1109 passing (was 1096; +13 — form-split/genre-rail unit
  tests, genre-view wire tests, the invalidation-race regression).
- **Netflix parity round 2 (spec #267, tickets #268–#271,
  2026-08-14).** (1) The detail «Схожі» shelf ranks the home snapshot
  by the #252 content-similarity scorer (deduped, item excluded, cold
  profiles fall back to genre matching) — genre-less items with a warm
  profile are no longer stuck empty. (2) The home snapshot persists to
  a versioned `home-snapshot.json` (knob `CS_UK_SNAPSHOT_PATH`, atomic
  writes, corrupt file → fresh build) so a cold start serves instantly
  at ANY age and heals in the background — the third ADR-0003
  persistence exception. (3) A «Нові серії» row at position 3 lists
  the series-form recent items whose merged groups appear in the
  viewer's playback history. Test counts: 1129 passing (was 1109;
  +20 — Similar profile tests, «Нові серії» builder/view tests,
  SnapshotStore unit + cold-start wire tests).
- **Netflix parity round 3 (spec #272, 2026-08-14).** (1) The person
  page works: the Items route honors `PersonIds` and matches against
  the #252 profile store's people, returning the person's movies and
  series from the home snapshot (filtered by `includeItemTypes`, the
  client splits films/series) — no new scraping, unknown person or
  cold store is a tolerant empty. (2) «Нещодавно переглянуто» sits at
  position 4 of home (after «Нові серії», before «Популярні зараз»):
  the most recently seen items, active AND finished — finished titles
  leave the resume shelf but stay browsable here, via a new finished-
  history section in the resume state file. Test counts: 1142 passing
  (was 1129; +13 — PersonIds filmography wire tests, «Нещодавно
  переглянуто» builder + view + finished-included tests, finished-
  history store tests).
- **Named dub picker + dub memory (spec #276, tickets #277–#279,
  2026-08-14).** (1) PlaybackInfo now serves ONE MediaSource per
  translation (cap 8, deduped by label) with an audio MediaStream
  carrying `Index` + `DisplayTitle` = the dub label — the client's
  named source picker replaces mpv's unnamed tracks for
  multi-translation content (issue #243 narrowed to single-
  translation dual-audio muxes only). Source order is dynamic: the
  picked `AudioStreamIndex` goes first, else the remembered dub.
  (2) The stream route accepts `mediaSourceId` (`<item>::<translation>`)
  and streams THAT translation — picking a source really switches the
  stream. (3) Per-series dub memory (LRU 50, newest wins, persisted in
  `user-state.json`; movies never remembered) makes the next play of a
  series default to the viewer's last dub. Test counts: 1151 passing
  (was 1142; +9 — multi-source PlaybackInfo wire tests, source-switch
  + memory tests, dub-memory store unit tests).
- **Switchfin dashboard surface (spec #280, tickets #281–#284,
  2026-08-14).** (1) The dashboard stops 404ing: `/Items/Counts`
  (movies/series from the home snapshot, episodes summed from cached
  content pages — never a fetch), `/System/Info/Storage` (real poster-
  cache footprint + free space, honest empty rows for the other
  folders), `/Users` (the single fixed facade user), and the graceful
  empties — `/ScheduledTasks`, `/Devices`, `/System/ActivityLog/
  Entries`, `/LiveTv/Programs/Recommended` — answer the client's
  standard zero envelopes while `POST /Sessions/Capabilities/Full`
  answers 204. (2) Original-quality Download: `GET /Items/{id}/
  Download` (the old 404, ticket #296) resolves the same stream seam
  as play and proxies the bytes with a `Content-Disposition` name
  `<safe-title>.<container>` — Cyrillic titles ride the RFC 5987
  `filename*=UTF-8''` form; the detail DTO carries the matching
  `MediaSources[].Name`. (3) `POST /System/Restart` answers 204 then
  re-execs via injectable seams (operator action, LAN-only). Danmaku
  and music/playlist are documented N/A (no such content in the
  catalog). Test counts: 1164 passing (was 1151; +13 — dashboard
  counts/storage/users wire tests, graceful-empties envelopes,
  Download bytes + disposition + 404, detail MediaSource name,
  restart seam tests).

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
uvicorn cs_uk_api.main:app --host 0.0.0.0 --port 8000
```

Run the tests:

```bash
cd backend && . .venv/bin/activate && pytest cs_uk_api/tests -v
```### PS4 client: Switchfin (2026-08-09)

The project is now **Switchfin** — a Jellyfin client on the PS4 — talking
 to the backend's Jellyfin facade (spec #100). The original C++ catalog
client and its Docker PS4 build pipeline were removed along with the
status sections that described them; the backend sections above remain
the current implementation status.

Search mapping (ticket #106): the facade serves the shared merged search
under `GET /Items?searchTerm=…` (both bare and `Users/{id}/Items`
spellings) and `GET /Search/Hints?searchTerm=…` — the same
`catalog_state.merged_search` core the native `/api/search` route runs
(one fan-out, one 5m cache, uakino skip/warming included). Search cards
carry `g1:` ids and Movie/Series types; the facade registers the merged
groups into the group-key resolution map so a searched card opens in the
detail/hierarchy surface (#105) even when it is not in the 30-min home
snapshot.

## Adding more providers

The v2 plan calls for 20 providers (issue #17). 19 are landed; 1 was
skipped (Banderakino — site offline). The Uakino provider is the
reference implementation and the sole JS-engine provider: its content
and player pages sit behind a Cloudflare Turnstile challenge, so the
plain-HTTP live gate cannot pass — the headless-Chromium session
(issues #193/#195) serves live requests instead. The API warms that
session in the background at startup (bounded by `WARM_WAIT_S`);
`/api/providers` reports `warming` while it is cold, `ok` once ready,
and the sliding-window health tracker recovers through the 5-minute
heartbeat. `refresh_uakino.py` is a detached external probe only — it
does not share state with the API process and answers whether a fresh
session can warm from zero on this host. See
[`docs/research/uakino-reachability-2026-08-02.md`](research/uakino-reachability-2026-08-02.md).
To add a new provider:

1. Create `backend/cs_uk_api/providers/<id>.py` implementing `BaseProvider`
   (`id`, `name`, `types`, `search`, `content`, `stream`, optionally
   `browse` and `episode_translations`).
2. Add fixtures in `backend/cs_uk_api/tests/fixtures/<id>/` and a
   `test_<id>.py` mirroring `test_ufdub.py` (the most recent reference).
   **Fixtures must be captured live via `curl -sS https://...`** —
   spec ground rule (no invented HTML).
3. Apply `re.fullmatch` slug validation at the start of `content()`
   and `stream()` — pattern follows the upstream Kotlin's path grammar
   (e.g. `r"\d+-[a-z0-9-]+"` or `r"[a-z0-9][a-z0-9-]*"`).
4. Use `from ..http_client import safe_get` for all `http.get` calls
   that follow URLs extracted from upstream HTML (SSRF defense — the
   helper validates the redirect target against an `allowed_hosts` set).
5. Use `from urllib.parse import quote` (or `quote_plus`) for any
   query-string parameter that may contain non-ASCII or reserved chars
   — never `.replace(' ', '+')`.
6. Register the provider in `backend/cs_uk_api/providers/_registry.py`
   by adding a `register(NewProvider())` line.
7. Update [`docs/provider-triage.md`](provider-triage.md) to flip the
   row from `TBD` to `ready`.
8. Smoke-test with `python -m cs_uk_api.scripts.live_gate --provider <id>`
   to confirm the stream plays in mpv on the live site.

No client changes are required for additional providers; Switchfin
consumes the Jellyfin facade, which serves whatever the registry
contains.
