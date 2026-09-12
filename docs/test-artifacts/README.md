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
| [`openclaw-home-smoke-2026-09-12.md`](openclaw-home-smoke-2026-09-12.md) | 2026-09-12 | **Post-merge deployment proof** for PRs #413–#417 on the operator's host: handshake, 17-view listing, 40-card browse, and a torrent-lane play end to end (PlaybackInfo → engine redirect target → `206` with 65536 bytes → `Content-Range` seek), plus the exit contract of `backend/deploy/smoke.sh`. Records three findings: the ~60s stall on a torrent-less title, an unreproduced transient detail-404 (filed as #420), and why an unbounded engine read is not a latency signal. |
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
