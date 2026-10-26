# Teleport threshold calibration — 2026-10-26

Real provider audio, spec #443. This note records the measurements used
to decide whether the initial `0.80 / 0.05 / 0.60` constants stay.

## Environment

- Host: the normal LAN backend host; `ffmpeg` 9.0.1.
- `fpcalc` 1.6.1 from the Arch `chromaprint` package. The host has no
  system install, so the package was extracted into the gitignored
  `backend/tools/fpcalc/` and put on `PATH` with its `LD_LIBRARY_PATH`.
- One reference clip per case: 15 s, decoded mono 11,025 Hz, ending at
  playback position 600 s. The production `ChromaprintAligner` then
  decoded the candidate from `position - 300` through `position + 315`
  and matched with the production matcher.

## Bug found before any calibration

`fpcalc` defaults to analyzing only the **first 120 seconds**. The
candidate window is ~615 s, so the reference at 585–600 s was never in
the fingerprinted audio. The control (same stream on both sides) failed:

| Before `-length 0` | Status | Confidence | Margin | Offset |
|---|---|---|---|---|
| kinotron *Game of Thrones* S1E1 TET → same stream | ambiguous | 0.6177 | 0.0066 | +145.896 s |

After adding `-length 0` to the worker, the same control:

| After `-length 0` | Status | Confidence | Margin | Offset |
|---|---|---|---|---|
| kinotron *Game of Thrones* S1E1 TET → same stream | matched | 0.9848 | 0.3456 | +0.019 s |

The fix is in the worker, not a threshold change. The opt-in real-tool
test now uses a 200 s source so this regression cannot pass again.

## Measurements

All offsets are positive means the alternate source is later. Status is
the production three-way verdict; only `matched` permits an auto-switch.

### Same-dub provider pairs

| Work / episode | Reference | Candidate | Status | Confidence | Margin | Offset |
|---|---|---|---|---|---|---|
| *Game of Thrones* S1E1 | kinotron `ТЕТ` | eneyida `ТЕТ` | matched | 0.9830 | 0.3438 | +0.019 s |
| *Game of Thrones* S1E1 | serialno `ТЕТ` | uaserialspro `ТЕТ` | matched | 0.9885 | 0.3520 | +0.019 s |
| *The Simpsons* S1E1 | kinotron `Дубляж` | serialno `Дубляж` | matched | 0.9853 | 0.3948 | +0.019 s |

### Different-dub pairs

The first two are provider pairs. The third is the hardest negative —
the same video with a different dub on the same host — and is included
because it is the case closest to the threshold.

| Work / episode | Reference | Candidate | Status | Confidence | Margin | Offset |
|---|---|---|---|---|---|---|
| *Клініка* S1E1 | kinotron `Цікава Ідея` | serialno `Paramount Comedy` | no_match | 0.5997 | 0.0060 | — |
| *Клініка* S1E1 | kinotron `Цікава Ідея` | uaflix `uk` | ambiguous | 0.6075 | 0.0138 | +278.351 s (suggestion only) |
| *Клініка* S1E1 | kinotron `Цікава Ідея` | kinotron `Колодій Трейлерів та місіс kor0bochkina` | ambiguous | 0.7279 | 0.1163 | +0.019 s (suggestion only) |

The control for the same work, kinotron `Цікава Ідея` → itself, was
`matched` at 0.9835 / 0.3810 / +0.019 s.

## Threshold decision

**Keep `MATCHED_CONFIDENCE = 0.80`, `MATCHED_MARGIN = 0.05`, and
`AMBIGUOUS_FLOOR = 0.60`.**

- The same-dub floor measured 0.9830; the different-dub ceiling measured
  0.7279. 0.80 sits in a 0.255-wide gap and does not need to move.
- The hard negative is rejected by confidence, not margin: its margin
  (0.1163) is above 0.05. Raising the margin would not make that case
  safer without also risking legitimate same-dub pairs, whose observed
  margins were 0.344–0.395. No measurement supports a change.
- The 0.60 ambiguous floor also behaved correctly: the serialno pair
  (0.5997) fell just below it to `no_match`; the uaflix pair (0.6075)
  rose just above it to `ambiguous`. Both forbid auto-switch, so the
  client-visible difference is only whether a suggested seek is shown.
- Latency per case was 8.0–16.7 s, inside the 45 s budget. These are
  single observations, not a p95; more samples are needed before making
  a latency claim.

## Reproduce / caveats

- The pair ids are in the tables; translations must be passed to
  `provider.stream()` directly for the different-dub cases. The teleport
  endpoint itself resolves the candidate's default track, so the
  different-dub check is a matcher-level calibration, not an endpoint
  acceptance run.
- Some providers list content-level translations that their player does
  not actually expose per episode (serialno *Game of Thrones* S1E1 has
  only `ТЕТ`, for example). Those fall back to the default track and
  must not be counted as different-dub cases.
- This is not the on-device acceptance gate. The client capture/switch
  remains unproven; #441 stays open.
