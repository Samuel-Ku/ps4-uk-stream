# Test artifacts — dated evidence from real runs

Durable record of runs against **real surfaces**: the live host, the real
BitPlay engine, a real Android client. These notes are what the words
"PROVEN" and "verified live" in the runbooks point at — a claim in
`docs/torrent-lane.md` or `backend/deploy/DEPLOY.md` should cite one of
these files, and a re-run should extend the existing note rather than
replace it.

**Convention:** `<topic>-<date>.md`, flat for cross-cutting proofs;
per-tool evidence (the device driver) lives in its own subdirectory.
Append a dated `## Re-run <date> — <what changed>` section when you
re-drive an existing proof, so the history stays in one place.

## Index

| Artifact | Date | What it proves |
|---|---|---|
| [`accept-373-2026-09-05.md`](accept-373-2026-09-05.md) | 2026-09-05, re-run 2026-09-06 | **#373 player floors** — engine F1–F4 (progressive start, Range seek, srt→VTT, audio) and facade B1–B5 (search → group key → playable id → PlaybackInfo → 302 → bytes → subtitles). The 09-06 re-run proves the `yts:engine` liveness probe and the stream-time retarget under a **live engine kill** (spec #394), including item-vs-lane isolation. The on-device PS4/Switchfin pass is the one stage handed to the operator. |
| [`teleport-thresholds-2026-10-26.md`](teleport-thresholds-2026-10-26.md) | 2026-10-26 (matcher, endpoint re-run, latency distribution, cold path) | **Teleport matcher calibration** (#443): real same-dub and different-dub provider audio, first at 600 s and then through the live endpoint at six positions (120–2400 s) plus different-dub checks on *The Big Bang Theory* and *Клініка*. Records the `fpcalc` 120 s default-truncation bug (fixed with `-length 0`), the measured 0.983 same-dub / 0.734 different-dub confidence gap that keeps `0.80 / 0.05 / 0.60` unchanged, and a 24-sample latency distribution (four passes × six positions on the serialno → uaserialspro pair): p95 14.2 s server / 14.6 s total, 24/24 `matched`. Six fresh-backend cold-path passes after the script's default flip matched 35/36 requests (one transient provider 502 at 900 s); the successful nearest-rank p95 was 17.1 s server / 17.8 s total at n=35. The on-device client gate is still separate. |
| [`teleport-endpoint-acceptance-2026-10-26.md`](teleport-endpoint-acceptance-2026-10-26.md) | 2026-10-26 | **Teleport live endpoint acceptance** (#443): one real `POST /api/teleport/align` on the calibrated GoT S1E1 kinotron `ТЕТ` → eneyida `ТЕТ` pair at 600 s. `matched` at +0.0186 s, confidence 0.9830, margin 0.3438, server alignment 15,820 ms, total request 16,132 ms. Proves the route/orchestration on live audio; the PS4 capture/switch gate remains open. |
| [`teleport-client-baseline-2026-10-26.md`](teleport-client-baseline-2026-10-26.md) | 2026-10-26 | **Teleport spike baseline (#450), partial**: pins Switchfin `0035d6b` and backend `f25b678`, proves a cold 200 `matched` route with `ffmpeg`/`fpcalc`, and shows the Jellyfin source list is translation-only, so the spike config must supply the candidate. Records the PS4 build/device blocker explicitly: no OpenOrbis toolchain, uninitialized `borealis`/`lunasvg`, Docker access denied, no console attached. |
| [`openclaw-home-smoke-2026-09-12.md`](openclaw-home-smoke-2026-09-12.md) | 2026-09-12, re-run 2026-10-26, 2026-10-27 (×2) | **Post-merge deployment proof** for PRs #413–#417 on the operator's host: handshake, 17-view listing, 40-card browse, and a torrent-lane play end to end (PlaybackInfo → engine redirect target → `206` with 65536 bytes → `Content-Range` seek), plus the exit contract of `backend/deploy/smoke.sh`. Records three findings: the ~60s stall on a torrent-less title, an unreproduced transient detail-404 (filed as #420), and why an unbounded engine read is not a latency signal. The 10-26 re-run proves the gate's dead-swarm failover at `e6c223d`: the default `Dune` run skipped `g3:tt35969257` after `000 0`, then played `g3:tt15239678` end to end (`9 passed, 0 failed, 1 skipped`, exit `0`). The 10-27 re-run proves the gate's 302 status check at `ac66df9` against the live process without a restart (same `9 passed, 0 failed, 1 skipped`, exit `0`). A second 10-27 run proves the five-flow gate at `29499b3`: the teleport flow reports the align route live (`400`) and `ffmpeg`/`fpcalc` resolvable in the service's own `PATH` (`11 passed, 0 failed, 1 skipped`, exit `0`). |
| [`switchfin/device-driving.md`](switchfin/device-driving.md) | since 2026-08-10 | **Operational knowledge** for driving the real Switchfin client over adb (`scripts/switchfin_test.py`) — what works, what does not, and why. Referenced by `run_backend_8003.sh`, `docs/architecture.md`, `docs/status.md`. |
| [`switchfin/report-2026-09-06-openclaw-home.md`](switchfin/report-2026-09-06-openclaw-home.md) | 2026-09-06 | Archived **device-sweep report** from the openclaw-home deployment. The live runner report (`docs/switchfin-test-report.md`) is gitignored, so this tracked copy is the record. |
| `switchfin/screen-play_*.png` | 2026-09-06 | Screenshots captured during that device sweep. |

## Not evidence — test fixtures

Two files here are **inputs to the test suite**, not notes. Editing them
changes what CI asserts, so treat a change to either as a test change:

- `switchfin/steps.yaml` and `switchfin/tap-coords.yaml` — read and pinned
  by `backend/cs_uk_api/tests/test_switchfin_runner.py` (step count, phases,
  per-branch reference lines, the verified auto-play shape) and consumed by
  `scripts/switchfin_test.py` on a real device.

`switchfin/capture/` is a **gitignored output directory** written by the
runner; nothing there is tracked.

## Re-running the proofs

- Deployment smoke (any host): `backend/deploy/smoke.sh` → new note here if
  the environment reveals something, as in the 2026-09-12 one.
- Ticket floors: `backend/deploy/accept_373.sh {engine|facade|all}`.
  Note it purges the engine cache — do not point it at a stack someone is
  using.
