# Implementation status

This document tracks what was delivered by the implementation pass that
followed the plan at
[`docs/superpowers/plans/2026-08-01-ps4-uk-stream-impl.md`](superpowers/plans/2026-08-01-ps4-uk-stream-impl.md).

## Delivered

### Backend (FastAPI, Python) -- 362 tests passing

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
  (issue #17). One skipped — `banderakino`, live site offline (HTTP 522)
  and the only provider not portable without a JS engine. The registered 19:
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
- Live gate tooling (`backend/scripts/live_gate.py`) drives
  search → content → stream → mpv playback against the real site for
  smoke-testing.
- Live smoke test confirmed `/api/providers` returns all registered
  providers and the validation/404 paths behave correctly.

Run the backend:

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
uvicorn cs_uk_api.main:app --host 0.0.0.0 --port 8000
```

Run the tests:

```bash
cd backend && . .venv/bin/activate && pytest cs_uk_api/tests -v
```

### pPlay fork catalog module -- 3/3 standalone tests passing

- `pplay-fork/src/catalog/Json.{h,cpp}` -- cJSON wrapper exposing
  `JsonDoc::parse`, `JsonValue::str`, `integer`, `arr`, `asArray`.
- `pplay-fork/src/catalog/CatalogApi.{h,cpp}` -- DTOs (`SearchItem`,
  `ContentItem`, `StreamInfo`) and pure parsing functions
  (`parseSearch`, `parseContent`, `parseStream`); the network
  methods call through an injected `HttpClient` interface (tests pass a
  fake; the real `BrowserHttpClient` wire-up was deferred at plan time
  and landed in Task 18).
- `pplay-fork/src/catalog/OnscreenKeyboard.{h,cpp}` -- UTF-8 aware
  on-screen keyboard widget with focus grid, `append(char32_t)`,
  `backspace()`, `clear()`, and action keys (`space`, `back`, `clear`,
  `done`).
- `pplay-fork/external/cJSON/` -- cJSON v1.7.18 vendored.
- `pplay-fork/tests/standalone-catalog/` -- sandbox-friendly CMake
  harness that builds only the catalog module + tests (the full pPlay
  build requires libcross2d/SDL2/ffmpeg/mpv which were not installed in
  the build environment).
- `pplay-fork/CMakeLists.txt` and `pplay-fork/tests/catalog/CMakeLists.txt`
  edited per the plan, ready to be picked up by a full pPlay build
  (cross2d is the missing dep on a non-PS4 Linux host).

Build and test the standalone harness:

```bash
cd pplay-fork
cmake -B build-standalone -S tests/standalone-catalog -DCMAKE_BUILD_TYPE=Debug
cmake --build build-standalone -- -j
ctest --test-dir build-standalone --output-on-failure
```

### PS4 build pipeline (Docker) -- scripts only, not run end-to-end

- `Dockerfile.ps4` -- Ubuntu 22.04 + clang/lld/cmake + OpenOrbis v0.5.2
  cloned to `/opt/oo` with the right env vars.
- `pplay-fork/scripts/build-ps4-docker.sh` -- autodetects docker/podman,
  builds the image, runs the FFmpeg + pPlay build, verifies artifacts.
- `pplay-fork/scripts/ffmpeg-ps4.sh` -- cross-compiles FFmpeg n6.1 for
  the OpenOrbis target.
- `pplay-fork/scripts/README.md` -- build, install, and test instructions
  for the user's own machine.

Syntactic verification (Task 19, sandbox-only, no Docker daemon):

- `bash -n pplay-fork/scripts/build-ps4-docker.sh` → OK
- `bash -n pplay-fork/scripts/ffmpeg-ps4.sh` → OK
- `Dockerfile.ps4` — 10 lines, valid FROM/RUN/ENV/WORKDIR/ENTRYPOINT
- `CMakeLists.txt` PLATFORM_PS4 branch sets `PS4_PKG_TITLE="pplay @
  cpasjuste"`, `PS4_PKG_TITLE_ID="PPLA00001"`, `PS4_PKG_VERSION=3.7`
- `libcross2d/cmake/targets.cmake` calls `add_self(${PROJECT_NAME})`
  and `add_pkg(... ${PS4_PKG_TITLE_ID} ${PS4_PKG_TITLE}
  ${PS4_PKG_VERSION})` (those macros ship with the OpenOrbis toolchain
  cmake helpers, exercised only at full Docker time)
- `data/ps4/romfs/` carries `sce_module/{libc.prx,libSceFios2.prx}`
  and `sce_sys/{icon0.png,about/right.sprx}` (the OpenOrbis toolchain
  synthesises param.sfo from the `PS4_PKG_*` CMake vars)

The Docker build was partially exercised in the sandbox (image layers
resolved, OpenOrbis cloned to `/opt/oo`); the final compile/link/PKG
step requires the actual OpenOrbis toolchain headers and a real Docker
daemon, neither of which the sandbox provided.

## Deferred -- work reserved for the user

These tasks are part of the plan but require the full pPlay tree
(libcross2d/SDL2/ffmpeg/mpv) and/or the OpenOrbis toolchain. They
should be done on a host that has both.

- **Task 19 -- End-to-end PS4 PKG build.** Run
  `./pplay-fork/scripts/build-ps4-docker.sh` on a Linux host with real
  Docker. Verify `pplay-fork/build/PPLA00001.pkg` exists, has the
  expected PKG magic, and is accepted by GoldHEN on the PS4.
- **Task 20 -- On-console test.** Install the PKG, fill in
  [`docs/ps4-test-report.md`](ps4-test-report.md), and tick the
  checklist.

## Tasks 18-20 (this pass)

- **Task 18 (done).** Full `ScreenSections`, `ScreenSearch`,
  `ScreenResults`, `ScreenContent` implementations wired through a
  shared `CatalogContext` (singleton-style accessor for one worker
  thread + one `BrowserHttpClient`). Main owns the `CatalogApi`
  lifecycle, adds a "Пошук UA" menu entry, and tears the api down
  before other screens in `~Main`. mpv ABI fix: dropped the obsolete
  third `mpv_opengl_init_params` initializer (libmpv 0.32+). Three
  out-of-class definitions in `Browser/regex.hpp`, `links.hpp`,
  `Browser.hpp` marked `inline` so two TUs can include them without
  multiple-definition errors.

  Linux build: `cmake --build build-linux --target pplay` → OK
  (ELF 64-bit produced). Standalone ctest: 3/3 pass. Backend pytest:
  362/362 pass.

- **Task 19 (syntactic verification only, see above).** Scripts and
  Dockerfile validated; final Docker-based PS4 PKG build still needs
  a host with Docker + the OpenOrbis toolchain (see Task 19 above).

- **Task 20 (done).** [`docs/ps4-test-report.md`](ps4-test-report.md)
  updated with the new "Пошук UA" menu entry, search → results →
  content → play happy-path   checklist, and Provider/Translation
  matrices for the 19 v2 providers.

## Adding more providers

The v2 plan calls for 20 providers (issue #17). 19 are landed; 1 was
skipped (Banderakino — site offline, and the only one not portable
without a JS engine). The Uakino provider is the reference
implementation — note: its live upstream moved to `uakino.best`
(content/player pages are behind a Cloudflare Turnstile challenge, the
site runs a new DLE theme), so the live gate cannot pass; fixture tests
remain green. See
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

No frontend changes are required for additional providers; the
`CatalogApi` client only consumes the API contract.
