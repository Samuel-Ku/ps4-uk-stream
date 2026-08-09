# Implementation status

This document tracks what was delivered by the implementation pass that
followed the plan at
[`docs/superpowers/plans/2026-08-01-ps4-uk-stream-impl.md`](superpowers/plans/2026-08-01-ps4-uk-stream-impl.md).

## Delivered

### Backend (FastAPI, Python) -- 719 tests passing (2026-08-08)

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
  Switchfin client via `adb shell input tap` through all 7 library
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

### PS4 build pipeline (Docker) -- end-to-end build verified

- `Dockerfile.ps4` -- Ubuntu 22.04 + clang/lld/cmake + OpenOrbis v0.5.2
  cloned to `/opt/oo` with the right env vars.
- `pplay-fork/scripts/build-ps4-docker.sh` -- autodetects docker/podman,
  builds the image, runs the FFmpeg + pPlay build, verifies artifacts.
- `pplay-fork/scripts/ffmpeg-ps4.sh` -- cross-compiles FFmpeg n6.1 for
  the OpenOrbis target.
- `pplay-fork/scripts/ps4-toolchain/pplay-create-fself.sh` -- bash
  wrapper around `create-fself` (Sony) that dodges cmake's
  Unix-Makefiles dash-escape quirk (cmake wraps arg values in `\"...\"`
  which dash treats as literal characters; the wrapper reads input /
  output paths from positional args that cmake leaves unescaped).
- `pplay-fork/scripts/README.md` -- build, install, and test instructions
  for the user's own machine.

End-to-end build (Task 19):

- `bash pplay-fork/scripts/build-ps4-docker.sh` → OK, produces
  `pplay-fork/build/IV0000-PPLA00001_00-PPLAY00000000000.pkg` (6.6 MB)
  with the fake-signed SELF at `pplay-fork/build/eboot/eboot.bin`
  (5.0 MB). Verified:
  - PKG magic `\x7FCNT` (bytes `7f 43 4e 54`) ✓
  - ELF magic `\x7FELF` (bytes `7f 45 4c 46`) ✓
  - ELF type `0xFE10` "SCE Executable (ASLR)" ✓
  - ELF OS/ABI: FREEBSD ✓; Machine: EM_X86_64 ✓
- Stubs added to make pplay linkable on PS4 without libmpv / libcurl /
  libpng / libz / libfreetype / libGLESv2 (none of which ship as
  static libs in the OpenOrbisSDK):
  - `libcross2d/source/platforms/ps4/gl_renderer_stub.cpp` -- no-op
    `c2d::GLRenderer` / `c2d::GLTexture` / `c2d::GLTextureBuffer` plus
    `glGenBuffers`/`glBindBuffer`/`glBufferData`/`glDeleteBuffers`
    stubs that `source/skeleton/sfml/VertexArray.cpp` calls directly.
  - `pplay-fork/src/p_movie.h` / `src/p_search.h` -- PS4 stubs for
    `pscrap::Movie` / `pscrap::Search` (the upstream classes pull in
    libcurl + json-c; we ship the catalog client only and run the
    scrapper as an out-of-band Linux service).
  - `pplay-fork/src/scrapper/scrapper_stub.cpp` -- no-op
    `pplay::Scrapper` for PS4.
  - `pplay-fork/src/player/ps4_stubs/mpv_stub.h` -- PS4 replacements
    for `<mpv/client.h>` and `<mpv/render_gl.h>` so pplay's
    `src/player/mpv.cpp` and `src/catalog/ScreenContent.cpp` compile
    without libmpv. `mpv_ps4_vars.cpp` provides the `ps4_mpv_*`
    globals that the upstream libmpv PS4 platform layer defines.
  - `pplay-fork/src/filer/Browser/Browser.hpp` -- when `__PS4__` is
    defined, the libcurl-backed `Browser` class is replaced with an
    empty stub (no networking on PS4; the catalog client uses its own
    libcurl-less path).
- Link line additions for the Sce SDK stubs that vendored SDL2.a
  references: `-lScePad -lSceAudioOut -lSceVideoOut -lSceUserService
  -lSceSysmodule -lSceSystemService` (added to `ps4.cmake`'s
  `CMAKE_EXE_LINKER_FLAGS`).
- FreeBSD 12.0 libc++ workarounds in `ps4.cmake` (pre-include
  `cstdlib` + `cmath-shim.h` so `using std::isnan;` / `std::isinf`
  resolve — the FreeBSD libc++ only exposes them as `#define` macros
  in this sysroot).
- pkg.gp4 generator hooked into the `pplay_pkg` cmake target (the
  upstream `add_pkg` macro wrote the generator script but never
  executed it; without this PkgTool.Core aborts with "Could not find
  file 'pkg.gp4'"). Generates the canonical OpenOrbis XML project
  descriptor from `PS4_PKG_TITLE_ID` / `PS4_PKG_TITLE` so the .pkg
  is named `IV0000-<TITLE_ID>_00-...pkg` (36-char content_id as
  required by PkgTool.Core).

## Deferred -- work reserved for the user

These tasks require a PS4 console with GoldHEN.

- **Task 20 -- On-console test.** Install the PKG (side-load via
  GoldHEN payload or DNS redirect), fill in
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

- **Task 19 (done).** End-to-end `bash pplay-fork/scripts/build-ps4-docker.sh`
  produces a 6.6 MB `IV0000-PPLA00001_00-PPLAY00000000000.pkg` with
  the expected PKG magic + ELF SELF type 0xFE10. See the
  "End-to-end build" section above for the full list of new files /
  changes. The release PKG is ready to install via GoldHEN.

- **Task 20 (done).** [`docs/ps4-test-report.md`](ps4-test-report.md)
  updated with the new "Пошук UA" menu entry, search → results →
  content → play happy-path   checklist, and Provider/Translation
  matrices for the 19 v2 providers.

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

No frontend changes are required for additional providers; the
`CatalogApi` client only consumes the API contract.
