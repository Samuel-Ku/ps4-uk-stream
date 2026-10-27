# Teleport client baseline — #450 (partial; PS4 hardware and toolchain blocked)

**Date:** 2026-10-26
**Ticket:** #450 (spike baseline for #441)
**Status:** **Partial. Not a PS4 pass.** The backend and source-list half was
verified; the stock PS4 build, device playback baseline, AO/rate, and mpv
package version remain blocked.

## Inputs pinned

| Input | Recorded value |
| --- | --- |
| Reference client | `dragonflylee/switchfin` at `0035d6bdd6da7a2c56992c77d2eee0b6e31d07bd` (`0035d6b`), clean working tree |
| Reference remotes | `origin https://github.com/dragonflylee/switchfin.git`, fetch and push |
| App fork/divergence | None: `backend/tools/switchfin` is the clean reference clone; no `teleport-spike` branch or fork edits exist in this checkout |
| Backend revision | `f25b678b29d31a363c542049c9a49a1fd7f18fdd` (`f25b678`), working tree dirty with the uncommitted cold-map teleport fix and calibration docs |
| Host tools | `ffmpeg` 9.0.1; `fpcalc` 1.6.1, run from `backend/tools/fpcalc/usr/bin` with `LD_LIBRARY_PATH=backend/tools/fpcalc/usr/lib` |
| Date | 2026-10-26 |

## Build and device probe

**PS4 build: blocked.**

- The reference tree has a `PLATFORM_PS4` target in `CMakeLists.txt`, including
  `add_self`/`add_pkg`, but configuration includes
  `library/borealis/cmake/toolchain.cmake` and the PS4 platform source from
  that submodule.
- `git submodule status` reports both submodules uninitialized:
  `library/borealis` (`0303fe7…`) and `library/lunasvg` (`2970a88…`). The
  PS4 platform source is therefore absent.
- The OpenOrbis toolchain commands are absent from `PATH`:
  `pacbrew-package`, `create-fself`, and `pkg-tool` all resolve to `missing`.
- Docker is installed, but the daemon is not reachable by this user:
  `permission denied while trying to connect to the Docker API at
  unix:///var/run/docker.sock`.
- No `.pkg` artifact exists anywhere in the checkout.
- The plan's expected package `ps4-openorbis-libmpv-0.36.0-2` remains an
  expectation, not a measured value.

**Console: blocked.**

- `adb devices -l` lists one Android device only:
  `192.168.2.121:37443 device product:alioth_eea model:M2012K11AG`.
- No PS4 target, GoldHEN endpoint, or console IP is recorded in the repository
  or exposed to this host. No PS4 FTP/payload connection was attempted.

Consequently the stock-package build, 60 s playback/seek/pause/exit baseline,
runtime AO, negotiated rate/channels, client API version, and audible-dropout
check were **not** performed.

## Backend route check (verified)

A backend was started on loopback with the repo-local audio tools:

```bash
cd backend
export PATH="$PWD/tools/fpcalc/usr/bin:$PATH"
export LD_LIBRARY_PATH="$PWD/tools/fpcalc/usr/lib"
uvicorn cs_uk_api.main:app --host 127.0.0.1 --port 8004
```

The teleport route is enabled by default (`teleport_enabled=True`). A cold
alignment with **no prior search or snapshot** returned a verdict:

| Pair / position | Status | Offset | Confidence | Margin | Request |
| --- | --- | --- | --- | --- | --- |
| serialno `ТЕТ` → uaserialspro `ТЕТ`, GoT S1E1 @ 600 s | matched | +0.0186 s | 0.9881 | 0.3520 | 23.11 s |

The request used the script's cold path (`--no-warm` at the time; cold is
now the default), so the result depends on the cold-map fix, not a warm
group map. The listener was
`127.0.0.1:8004`; the deployment unit runs `--host 0.0.0.0`, so LAN/WAN
exposure was not proven here. The route has no authentication by design, and
only a host firewall keeps it LAN-only.

## Source-list check (verified, with a production prerequisite)

Live `POST /Items/{id}/PlaybackInfo` responses were inspected through the
Jellyfin facade:

| Item | MediaSources | Source id | Path |
| --- | --- | --- | --- |
| `serialno:28-gra-prestoliv:s1e1` | 1 | `serialno:28-gra-prestoliv:s1e1` | `/Videos/serialno:28-gra-prestoliv:s1e1/stream` |
| `ufdub:serial-423-knygarnia-bleka-sezon-1:s1e1` | 1 | same item id | `/Videos/…/stream` |

Both sampled sources carry the played item's own id, not another provider's
episode id. The facade's multi-translation path names sources
`<item_id>::<translation_id>` (`_multi_source_media_sources` in the Jellyfin
playback-info module), so even multiple entries are **dub translations of the
same provider item**, not cross-provider episodes.

**Production prerequisite:** the fork's existing source list does not yield a
provider-scoped alternate episode id. The spike must use its config file for
the candidate. The calibrated candidate is:

```text
uaserialspro:274-gra-prestoliv-s2011:s1e1
```

for reference `serialno:28-gra-prestoliv:s1e1` (both *Game of Thrones* S1E1,
`ТЕТ`).

## #450 acceptance status

| Criterion | Status | Evidence / blocker |
| --- | --- | --- |
| Stock PS4 package builds and plays 60 s with seek/pause/exit | **Blocked** | No OpenOrbis tools, uninitialized Borealis/lunasvg submodules, Docker daemon denied, no PS4 connected |
| LAN backend answers with teleport enabled and `ffmpeg`/`fpcalc` | **Partial** | Cold 200 `matched` on loopback with both tools present; LAN/WAN boundary unproven |
| Runtime AO, rate/channels, libmpv/client API version recorded | **Blocked** | No PS4 runtime; only the plan's expected `ps4-openorbis-libmpv-0.36.0-2` is known |
| Provider-scoped playable episode id recorded, or source-list limitation + config candidate | **Met with limitation** | Candidate id recorded; live source list is translation-only, so the spike config must supply it |
| App build, mpv package, backend revision, date recorded | **Partial** | App reference `0035d6b`, backend `f25b678` + dirty fix, date recorded; PS4 package/build hash does not exist |

## What a full #450 pass still needs

1. A PS4 on the LAN running FW 11.00 + GoldHEN with Switchfin installed, or a
   network path to it, plus its IP.
2. An initialized `library/borealis` and `library/lunasvg` tree and the
   OpenOrbis/pacbrew toolchain (or a working PS4 build container), so the
   stock package can be built from `0035d6b` and its hash recorded.
3. A 60 s playback/seek/pause/exit run with the runtime AO, negotiated
   audio parameters, and `mpv_client_api_version()` captured from the device.

## Reproduce the verified half

```bash
# backend, from backend/ with the repo-local fpcalc on PATH/LD_LIBRARY_PATH
uvicorn cs_uk_api.main:app --host 127.0.0.1 --port 8004

# cold verdict, no prior search
python3 ../scripts/teleport_calibrate.py \
  'serialno:28-gra-prestoliv:s1e1' \
  'uaserialspro:274-gra-prestoliv-s2011:s1e1' \
  --position 600 --base-url http://127.0.0.1:8004

# source list (`CS_UK_JF_TOKEN` defaults to the dev token)
curl -X POST http://127.0.0.1:8004/Items/serialno:28-gra-prestoliv:s1e1/PlaybackInfo \
  -H "X-Emby-Token: $CS_UK_JF_TOKEN" -H 'Content-Type: application/json' -d '{}'
```

No stream URLs, provider headers, audio bytes, or credentials are recorded in
this artifact.
