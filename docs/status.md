# Implementation status

This document tracks what was delivered by the implementation pass that
followed the plan at
[`docs/superpowers/plans/2026-08-01-ps4-uk-stream-impl.md`](superpowers/plans/2026-08-01-ps4-uk-stream-impl.md).

## Delivered

### Backend (FastAPI, Python) -- 167 tests passing

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
- **9 providers landed** in `backend/cs_uk_api/providers/` (issue #17,
  v2 scope of 20):
  - `uakino` (reference impl), `ufdub`, `unimay`, `kinotron`, `cikavaideya`,
    `hentaiukr`, `bambooua`, `kinovezha`, `animeua`
  - Each has its own `test_<id>.py` with 9–15 tests using `respx`-mocked
    fixtures. 11 v2 providers still to land; see
    [`docs/provider-triage.md`](provider-triage.md).
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
  (`parseSearch`, `parseContent`, `parseStream`); the async network
  methods are stubs pending Task 13.
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

The Docker build was partially exercised in the sandbox (image layers
resolved, OpenOrbis cloned to `/opt/oo`); the final compile/link/PKG
step requires the actual OpenOrbis toolchain headers and a real Docker
daemon, neither of which the sandbox provided.

## Deferred -- work reserved for the user

These three tasks are part of the plan but require the full pPlay tree
(libcross2d/SDL2/ffmpeg/mpv) and/or the OpenOrbis toolchain. They
should be done on a host that has both.

- **Task 13 -- Wire `CatalogApi` to the existing `Browser`** in
  `pplay-fork/src/filer/Browser/Browser.hpp`. The plan provides the
  three async method bodies and the two static helpers
  (`Browser::lastResponse()`, `Browser::lastError()`) to add to that
  file. The standalone tests continue to pass; on a full Linux build of
  pPlay, this completes the network plumbing.
- **Task 14 -- Config option and main-menu entry.** Add `OPT_CATALOG_URL`
  to `pplay-fork/src/pplay_config.h`, the default value to
  `pplay-fork/src/pplay_config.cpp`, the "Каталог UA" item to
  `pplay-fork/src/menus/menu_main.cpp`, the routing branch, and a
  placeholder `pplay-fork/src/catalog/ScreenSearch.{h,cpp}` so the
  build links. The default URL should be the user's LAN IP.
- **Task 15 -- Full `ScreenSearch`, `ScreenResults`, `ScreenContent`.**
  Replace the placeholder `ScreenSearch` with the full version from
  the plan (uses `OnscreenKeyboard`, calls `CatalogApi::searchAsync`,
  pushes `ScreenResults` on success). Implement the other two screens
  per the plan. Wire `ScreenContent` to hand the resolved URL to the
  existing `Player::load()` -- the current implementation just prints
  the URL to stderr pending the real handoff in the pPlay `main.cpp`
  scene stack.
- **Task 16 -- End-to-end PS4 PKG build.** Run
  `./pplay-fork/scripts/build-ps4-docker.sh` on a Linux host with real
  Docker. Verify `pplay-fork/build/PPLA00001.pkg` exists, has the
  expected PKG magic, and is accepted by GoldHEN on the PS4.
- **Task 17 -- On-console test.** Install the PKG, fill in
  [`docs/ps4-test-report.md`](ps4-test-report.md), and tick the
  checklist.

## Adding more providers

The v2 plan calls for 20 providers (issue #17). 9 are landed; 11
remain. The Uakino provider is the reference implementation. To add a
new provider:

1. Create `backend/cs_uk_api/providers/<id>.py` implementing `BaseProvider`
   (`id`, `name`, `types`, `search`, `content`, `stream`, optionally
   `browse` and `episode_translations`).
2. Add fixtures in `backend/cs_uk_api/tests/fixtures/<id>/` and a
   `test_<id>.py` mirroring `test_ufdub.py` (the most recent reference).
3. Register the provider in `backend/cs_uk_api/providers/_registry.py`
   by adding a `register(NewProvider())` line.
4. Update [`docs/provider-triage.md`](provider-triage.md) to flip the
   row from `TBD` to `ready`.
5. Smoke-test with `python -m cs_uk_api.scripts.live_gate --provider <id>`
   to confirm the stream plays in mpv on the live site.

No frontend changes are required for additional providers; the
`CatalogApi` client only consumes the API contract.
