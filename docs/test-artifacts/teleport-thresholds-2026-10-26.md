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

## Threshold decision (initial sample)

**Keep `MATCHED_CONFIDENCE = 0.80`, `MATCHED_MARGIN = 0.05`, and
`AMBIGUOUS_FLOOR = 0.60`.** The live endpoint re-run below extends this
sample and keeps the same constants; its negative ceiling is 0.7339.

- The same-dub floor measured 0.9830; the different-dub ceiling measured
  0.7279. 0.80 sits in a 0.255-wide gap and does not need to move.
- The hard negative is already rejected by confidence. Its margin
  (0.1163) is above 0.05, so the margin gate alone would not reject it;
  a higher margin (e.g. 0.15) could add a second gate, and the observed
  same-dub margins (0.344–0.395) leave room. The confidence gate already
  separates this sample, however, and no same-dub case was measured near
  0.80. The constants are left unchanged rather than tuned to three
  negatives.
- The 0.60 ambiguous floor also behaved correctly: the serialno pair
  (0.5997) fell just below it to `no_match`; the uaflix pair (0.6075)
  rose just above it to `ambiguous`. Both forbid auto-switch, so the
  client-visible difference is only whether a suggested seek is shown.
- Latency per case was 8.0–16.7 s, inside the 45 s budget. These are
  single observations, not a p95; more samples are needed before making
  a latency claim. The 24-sample distribution below supersedes this for
  the serialno → uaserialspro pair.

## Reproduce / caveats

The repeatable wrapper is `scripts/teleport_calibrate.py`. It resolves
both playable ids, captures each reference clip, and posts it on the cold
path (no prior search), then prints the verdict table:

```bash
python3 scripts/teleport_calibrate.py \
  'kinotron:9709-gra-prestolv:s1e1' \
  'eneyida:series/1136-gra-prestoliv-2001-b:s1e1' \
  --position 120 --position 600 --position 1500
```

Add `--json-out PATH` for the raw verdicts or `--clip-dir DIR` to keep
the captured WAVs.

- The pair ids are in the tables. These initial different-dub cases
  passed translations to `provider.stream()` directly, because the
  teleport endpoint resolves the candidate's default track; the re-run
  below instead drives the endpoint with an explicit reference clip.
- Some providers list content-level translations that their player does
  not actually expose per episode (serialno *Game of Thrones* S1E1 has
  only `ТЕТ`, for example). Those fall back to the default track and
  must not be counted as different-dub cases.
- This is not the on-device acceptance gate. The client capture/switch
  remains unproven; #441 stays open.

## Re-run 2026-10-26 — multi-position same-dub and endpoint different-dub checks

This re-run exercises the live `POST /api/teleport/align` route, not the
aligner seam. Backend `127.0.0.1:8003`, HEAD `f25b678`, same `fpcalc`
1.6.1 / `ffmpeg` 9.0.1 as above. Each reference was a 15 s clip extracted
from provider A from 15 s before the reported position, 48 kHz stereo
16-bit PCM (measured 14.991–15.000 s).

**Cold-cache prerequisite.** The route derives its work/episode equality
check from the group resolution map. A freshly restarted backend answered
400 `both ids must be series episodes` for the valid GoT pair until a
`/api/search` request registered both items. Warm the pair (search or
home snapshot) before calibrating; this is an orchestration caveat, not a
matcher result. A related edge: a group with two kinotron items registers
only one of them, so `kinotron:906-klinika:s1e1` stayed a 400 while
`kinotron:10070-klnka:s1e1` worked. That is why the *Клініка* row below
uses the latter.

**Fixed 2026-10-26.** The route now resolves the two content pages when
the group map is cold, so a valid pair no longer needs a prior search;
the 400 above is the pre-fix behavior. The calibration script now uses
this cold path by default; `--warm` restores the title-search warm-up for
older backends. The fix was verified on a fresh backend with the script's
then-opt-out `--no-warm` (now the default): serialno → uaserialspro
*Game of Thrones* S1E1 at 600 s was `matched` at +0.0186 s / 0.9881 /
0.3520 in 14.09 s.

### Same-dub across the episode

Reference: kinotron *Game of Thrones* S1E1 `ТЕТ`; candidate: eneyida
S1E1 `ТЕТ`. The 600 s row re-runs the endpoint acceptance above.

| Position | Status | Offset | Confidence | Margin | Server ms | Request s |
|---|---|---|---|---|---|---|
| 120 s | matched | −0.0062 s | 0.9876 | 0.3272 | 13,103 | 13.391 |
| 300 s | matched | +0.0177 s | 0.9830 | 0.3612 | 12,451 | 12.774 |
| 600 s | matched | +0.0186 s | 0.9830 | 0.3438 | 14,495 | 15.056 |
| 900 s | matched | +0.0186 s | 0.9858 | 0.3332 | 17,601 | 17.866 |
| 1500 s | matched | +0.0139 s | 0.9949 | 0.3520 | 15,742 | 16.117 |
| 2400 s | matched | +0.0186 s | 0.9862 | 0.3690 | 13,051 | 13.339 |

All six positions were `matched`. Confidence stayed in 0.9830–0.9949 and
margin in 0.3272–0.3690; the offset spread was 24.8 ms (−0.0062 to
+0.0186 s). The 600 s re-run reproduced the earlier acceptance values
(0.9830 / 0.3438 / +0.0186 s) at 14.495 s of server alignment instead of
15.820 s. No position moved a metric toward a gate.

### Different-dub checks

The *Big Bang Theory* (BBT) pair is new to this note: the reference is
kinotron's explicit `Інтер` track, while serialno exposes a single S1E1
track labelled `Paramount Comedy` at content level. The *Клініка* pair
uses kinotron's default `Колодій Трейлерів та місіс kor0bochkina` track
against serialno's single S1E1 track; its content labels do not identify
the one per-episode file, so the track names below are deliberately not
overstated.

| Work / episode | Reference track | Candidate | Position | Status | Confidence | Margin | Offset (suggestion only) | Server ms | Request s |
|---|---|---|---|---|---|---|---|---|---|
| *The Big Bang Theory* S1E1 | kinotron `Інтер` | serialno `Paramount Comedy` | 300 s | ambiguous | 0.6199 | 0.0188 | +0.0186 s | 16,090 | 16.710 |
| *The Big Bang Theory* S1E1 | kinotron `Інтер` | serialno `Paramount Comedy` | 600 s | ambiguous | 0.7339 | 0.1016 | +0.0186 s | 13,305 | 13.900 |
| *The Big Bang Theory* S1E1 | kinotron `Інтер` | serialno `Paramount Comedy` | 1200 s | ambiguous | 0.6461 | 0.0230 | −0.8115 s | 10,361 | 10.897 |
| *Клініка* S1E1 | kinotron `Колодій Трейлерів` | serialno single track | 600 s | ambiguous | 0.6080 | 0.0119 | +240.5881 s | 10,747 | 11.339 |
| *Клініка* S1E1 | serialno single track | kinotron `Колодій Трейлерів` | 600 s | ambiguous | 0.6034 | 0.0028 | −226.5600 s | 13,771 | 14.854 |

Every different-dub row is `ambiguous`, never `matched`, and the two
large *Клініка* offsets are matcher noise that only exist because
`ambiguous` carries a suggestion. The new rows span 0.6034–0.7339:
BBT at 600 s is the highest, *Клініка* reversed the lowest.

### Decision after the re-run

**Keep `MATCHED_CONFIDENCE = 0.80`, `MATCHED_MARGIN = 0.05`, and
`AMBIGUOUS_FLOOR = 0.60`.**

- The same-dub floor is 0.9830 and the highest different-dub confidence
  is 0.7339 — a 0.249-wide gap. 0.80 sits 0.066 above the negative
  ceiling and 0.183 below the same-dub floor, so the measurements still
  support the existing match gate. No same-dub position came near it.
- The BBT 600 s margin (0.1016) is above 0.05, so the margin gate alone
  would not reject that negative; confidence is the discriminator. The
  same-dub margins (0.327–0.369) leave ample room, but there is no
  evidence that raising the margin adds rejection power here.
- The ambiguous floor is tighter than the earlier three-negative sample
  suggested: 0.6034 and 0.6080 are only 0.003–0.008 above it, and both
  carry a nonsense seek. A 0.65 floor would turn the two *Клініка* rows,
  BBT 300, and BBT 1200 into `no_match`, but BBT 600 (0.7339) would
  remain `ambiguous`; a 0.75 floor would suppress all five negative rows
  while leaving 0.233 of headroom below the same-dub floor. The constants
  are left unchanged on these five new negative rows because `ambiguous`
  never auto-switches and the client must confirm the seek, but this is
  the first evidence that the 0.60 floor can surface a bad suggestion. If
  the client confirmation screen makes that suggestion prominent,
  revisit the floor before enabling the feature.
- Server alignment latency was 10.361–17.601 s and total request time
  10.897–17.866 s across the eleven requests. All are inside the 45 s
  budget; the 24-sample distribution below gives a p95 for one pair.
- Limits are unchanged: the clips were extracted server-side with
  `ffmpeg`, not captured from the PS4's decoded audio, and the multi-position
  same-dub evidence is still one provider pair. This is not the #441 client
  gate.

## Re-run 2026-10-26 — latency distribution, four passes × six positions

Same live route, backend `127.0.0.1:8003`, HEAD `f25b678`, `fpcalc` 1.6.1,
`ffmpeg` 9.0.1. Pair: serialno *Game of Thrones* S1E1 `ТЕТ` →
uaserialspro S1E1 `ТЕТ` (the second calibrated same-dub pair in the table
above; its 600 s measurement was 0.9885 / 0.3520 / +0.019 s).

**Why not the kinotron pair.** The first two passes were started on the
kinotron `ТЕТ` → eneyida `ТЕТ` pair used by the earlier re-run. Pass 1
completed 6/6 `matched`; pass 2 failed before any align request with
`stream kinotron:9709-gra-prestolv:s1e1: HTTP 502 upstream_unreachable`,
and a retry hit upstream timeouts. No kinotron samples from this window
are used in the distribution below. The switch is a provider-availability
substitution, not a matcher change.

**Method.** Four passes of the same six positions (120, 300, 600, 900,
1500, 2400 s), with the order varied per pass: ascending, descending, and
two different shuffles. Each position is one live `POST /api/teleport/align`
with a freshly captured 15 s reference clip; nothing is cached or replayed
between passes. All 24 requests returned HTTP 200, and all 24 route log
rows pair one-to-one with the script JSON by offset (checked exactly).

### Verdicts

All 24 were `matched`; there were no `ambiguous` or `no_match` rows.
Each position returned bit-identical offset, confidence, and margin in all
four passes, so the per-position values below have no spread.

| Position | Passes | Status | Offset | Confidence | Margin | Server s | Request s |
|---|---|---|---|---|---|---|---|
| 120 s | 4 | matched | −0.0062 s | 0.9867 | 0.3272 | 11.10–14.07 | 11.53–14.63 |
| 300 s | 4 | matched | +0.0186 s | 0.9848 | 0.3653 | 11.32–14.17 | 11.78–14.56 |
| 600 s | 4 | matched | +0.0186 s | 0.9881 | 0.3520 | 11.54–13.49 | 12.43–13.96 |
| 900 s | 4 | matched | +0.0186 s | 0.9867 | 0.3350 | 12.16–13.72 | 12.46–14.15 |
| 1500 s | 4 | matched | +0.0186 s | 0.9899 | 0.3465 | 12.52–13.38 | 12.96–13.80 |
| 2400 s | 4 | matched | +0.0434 s | 0.9172 | 0.2882 | 10.93–14.96 | 11.34–15.39 |

Across all positions the offset range was 49.6 ms (−0.0062 to +0.0434 s),
confidence 0.9172–0.9899, and margin 0.2882–0.3653. The 2400 s position is
the weakest on both gates: 0.117 above `MATCHED_CONFIDENCE` and 0.238 above
`MATCHED_MARGIN`. Repeating the same position does not move the verdict, so
the remaining variation in the table is transport time only.

### Latency distribution (n = 24)

`Server` is the route's own `latency_ms` log field (decode + fingerprint +
match). `Request` is the script's wall clock for the align POST. `p95` is
nearest-rank (23rd of 24), not interpolated.

| Metric | min | median | mean | p95 | max |
|---|---|---|---|---|---|
| Server alignment (s) | 10.934 | 13.303 | 12.994 | 14.166 | 14.965 |
| Total request (s) | 11.338 | 13.747 | 13.448 | 14.629 | 15.395 |
| Client/HTTP overhead (s) | 0.295 | 0.433 | 0.455 | 0.559 | 0.895 |

Both distributions sit well inside the 45 s budget at every sample, with a
p95 of 14.2 s of server alignment. The spread is about 4 s end to end. The
first timed request after the group-map warm-up (14.63 s total / 14.07 s
server) is not distinguishable from the rest, so this run gives no evidence
of an additional cold-start penalty once the pair is registered.

### Decision

**Keep `MATCHED_CONFIDENCE = 0.80`, `MATCHED_MARGIN = 0.05`, and
`AMBIGUOUS_FLOOR = 0.60`; no code change.** The weakest same-dub observation
here is 0.9172 confidence / 0.2882 margin, still 0.117 clear of the match
gate, and the repeated passes show the verdict is deterministic for a given
position. The 45 s budget is met with a 14.2 s p95 on this pair.

Limits: 24 observations from four passes over one same-dub pair on one host;
four samples per position is enough for a range, not a per-position p95. The
passes share the same CDN/HLS path, so they do not capture provider-side
cold segments or congestion. The clips are server-extracted, not PS4 decoded
audio, so this remains latency evidence for the endpoint, not the #441
client gate.

## Cold-path default verification — 2026-10-26

This run checks `scripts/teleport_calibrate.py` after its default flipped to
the cold path. It is not a matcher change and not a new latency
distribution; it proves the now-default invocation works without a prior
search.

### Method

- Backend `f25b678` plus the uncommitted cold-map fix, loopback
  `127.0.0.1:8014`, started 2026-10-26 21:12 CET.
- The process started with `CS_UK_CATALOG_WARM=0`,
  `CS_UK_SNAPSHOT_PATH=/tmp/cs-uk-cold-snapshot.json` (absent at start),
  and isolated resume/user-state paths. `/api/health` reported
  `catalog_warm.status=done`, `home_warmed=false`, `content_warmed=0`, and
  `planned=0`, and the snapshot file stayed absent. The group map therefore
  began empty.
- No `/api/search` request reached the process during the run. With the
  group map empty, the route resolves the pair through the cached content
  pages; the log shows content-page reads, not a search warm-up.
- Invocation (no `--warm`; the header printed `path cold`):

```bash
python3 scripts/teleport_calibrate.py \
  'serialno:28-gra-prestoliv:s1e1' \
  'uaserialspro:274-gra-prestoliv-s2011:s1e1' \
  --position 120 --position 300 --position 600 \
  --position 900 --position 1500 --position 2400 \
  --base-url http://127.0.0.1:8014
```

`Server` is the route's `latency_ms` field; `Request` is the script's align
POST wall clock.

### Verdicts and latency

| Position | Status | Offset | Confidence | Margin | Server | Request |
|---|---|---|---|---|---|---|
| 120 s | matched | −0.0062 s | 0.9867 | 0.3272 | 14.100 s | 15.136 s |
| 300 s | matched | +0.0186 s | 0.9848 | 0.3653 | 16.050 s | 16.503 s |
| 600 s | matched | +0.0186 s | 0.9881 | 0.3520 | 14.681 s | 15.237 s |
| 900 s | matched | +0.0186 s | 0.9867 | 0.3350 | 17.084 s | 17.801 s |
| 1500 s | matched | +0.0186 s | 0.9899 | 0.3465 | 14.288 s | 14.829 s |
| 2400 s | matched | +0.0434 s | 0.9172 | 0.2882 | 17.403 s | 17.995 s |

| Metric | Value |
|---|---|
| Verdicts | 6/6 HTTP 200, all `matched` |
| Offset | −0.0062 to +0.0434 s |
| Confidence | 0.9172–0.9899 |
| Margin | 0.2882–0.3653 |
| Server alignment | 14.100–17.403 s (median 15.366 s) |
| Total request | 14.829–17.995 s (median 15.870 s) |

All six requests are inside the 45 s budget. The first request was not the
slowest (15.136 s total / 14.100 s server), so this pass shows no extra
cold-start penalty after the initial stream resolution; it does not test
provider-side cold CDN segments. The weakest row is still 2400 s at 0.9172
confidence / 0.2882 margin, matching the earlier distribution.

### Repeat passes and spread

Two more passes used the same six positions, each against its own freshly
started backend with `CS_UK_CATALOG_WARM=0`, an absent snapshot path, and
isolated state files. Both reported `home_warmed=false` /
`content_warmed=0`; neither created a snapshot nor logged an `/api/search`
request. Pass 2 returned five `matched` rows and one **502
`candidate_unavailable`** at 900 s; pass 3 returned 6/6 `matched`.

| Position | Verdicts | Request range (median) | Server range (median) |
|---|---|---|---|
| 120 s | 3/3 matched | 11.672–15.136 s (13.433) | 10.809–14.100 s (12.592) |
| 300 s | 3/3 matched | 11.580–16.503 s (12.864) | 11.165–16.050 s (12.393) |
| 600 s | 3/3 matched | 11.735–15.237 s (13.206) | 11.318–14.681 s (12.743) |
| 900 s | 2/3 matched (one 502) | 15.674–17.801 s (16.738) | 15.261–17.084 s (16.172) |
| 1500 s | 3/3 matched | 13.667–17.668 s (14.829) | 13.032–14.288 s (13.203) |
| 2400 s | 3/3 matched | 14.081–17.995 s (15.842) | 11.989–17.403 s (13.665) |

The 900 s ranges cover the two successful samples; the failed request took
11.268 s and never reached alignment. The backend log names the cause
`playback stream failed provider=uaserialspro ... err=unreachable`, so it is
a provider/content failure, not a matcher result. The next fresh pass at
the same position matched with the same values as pass 1.

All successful verdicts were identical per position across passes: the same
offset, confidence, and margin every time. Only latency varied.

| Metric (17 successful requests) | min | median | mean | p95 | max |
|---|---|---|---|---|---|
| Server alignment | 10.809 s | 13.203 s | 13.634 s | 17.403 s | 17.403 s |
| Total request | 11.580 s | 14.829 s | 14.643 s | 17.995 s | 17.995 s |

`p95` is nearest-rank (17th of 17), so at this sample size it is the maximum
observation, not a stable tail estimate. All 17 successes stayed inside the
45 s budget.

Limits: three passes over one same-dub pair on one host, loopback only;
17/18 requests succeeded. The clips were extracted server-side with
`ffmpeg`, not captured from PS4 decoded audio, so this remains route
evidence, not the #441/#450 device gate.

### Six-pass update — passes 4–6

Three further passes (4–6) each used a fresh backend under the same
`CS_UK_CATALOG_WARM=0`, absent-snapshot, and isolated-state setup. All three
returned 6/6 `matched`, including 900 s. Combined with passes 1–3, the cold
path now has **35 matched requests out of 36**; pass 2's 900 s provider 502
remains the only miss.

| Position | Matched | Request range (median) | Server range (median) |
|---|---|---|---|
| 120 s | 6/6 | 11.608–15.136 s (13.105) | 10.809–14.100 s (12.346) |
| 300 s | 6/6 | 11.580–16.503 s (12.948) | 11.165–16.050 s (12.504) |
| 600 s | 6/6 | 11.735–15.237 s (12.964) | 11.318–14.681 s (12.516) |
| 900 s | 5/6 | 10.719–17.801 s (13.517) | 10.276–17.084 s (13.044) |
| 1500 s | 6/6 | 12.304–17.668 s (14.094) | 11.893–14.288 s (13.117) |
| 2400 s | 6/6 | 13.347–17.995 s (13.747) | 11.989–17.403 s (12.909) |

| Metric (35 successful requests) | min | median | mean | p95 | max |
|---|---|---|---|---|---|
| Server alignment | 10.276 s | 12.743 s | 13.058 s | 17.084 s | 17.403 s |
| Total request | 10.719 s | 13.412 s | 13.805 s | 17.801 s | 17.995 s |

`p95` is nearest-rank (34th of 35), the second-highest observation. This
supersedes the three-pass p95 above, which was the maximum at n=17. The 900 s
slot now has five successful samples; its failed request took 11.268 s and
never reached alignment. All successful verdicts were again identical per
position, and all 35 successes stayed inside the 45 s budget.

Limits: six passes over one same-dub pair on one host, loopback only;
900 s has five successes and one provider 502. The clips were extracted
server-side with `ffmpeg`, not captured from PS4 decoded audio, so this
remains route evidence, not the #441/#450 device gate.
