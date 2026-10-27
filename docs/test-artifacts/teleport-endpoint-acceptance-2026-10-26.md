# Teleport endpoint acceptance — 2026-10-26

**Ticket:** #443 backend contract, parent #441. **Scope:** one live
`POST /api/teleport/align` request against the real route, with a real
15-second reference clip from provider A and provider B as the candidate.
**Status:** endpoint acceptance **PASS** for this pair. This is not the
on-device client gate; no decoded-audio capture, stream switch, or seek was
exercised on the PS4.

## Environment

- Backend at `127.0.0.1:8003`, HEAD `f25b678` (endpoint `f9c4547`,
  full-window `fpcalc` fix `840cb7d`).
- `CS_UK_TELEPORT_ENABLED=1`; `ffmpeg` 9.0.1; `fpcalc` 1.6.1 from the
  extracted Arch `chromaprint` package under `backend/tools/fpcalc/`.
- Request time is the wall-clock total from `curl`; server time is the
  request middleware line; alignment latency is the route's own
  `latency_ms` (from candidate resolution completion through the aligner).

## Pair and reference

| Field | Value |
| --- | --- |
| Work / episode | *Game of Thrones* S1E1 |
| Reference (`item_id`) | `kinotron:9709-gra-prestolv:s1e1`, translation `ТЕТ` |
| Candidate (`candidate_id`) | `eneyida:series/1136-gra-prestoliv-2001-b:s1e1`, translation `ТЕТ` |
| Reference position | 600.000 s |
| Clip | 15.000 s, 48 kHz, stereo, 16-bit PCM WAV, 2,880,078 bytes |
| Upload | 2,880,801 bytes (multipart overhead included) |

The clip was extracted from A with `ffmpeg -ss 585 -t 15`, using the
provider headers returned by `/api/stream/{item_id}`. `ffprobe` reports
`duration=15.000000`, `pcm_s16le`, 48 kHz, 2 channels.

## Result

`HTTP 200` at `2026-10-26 19:20:49` local.

| Metric | Value |
| --- | --- |
| `status` | `matched` |
| `offset_seconds` | **+0.018572** |
| `confidence` | **0.982996** |
| `margin` | **0.343750** |
| `seek_ticks` | 6,000,185,720 (600.018572 s) |
| `source_id` | `eneyida:series/1136-gra-prestoliv-2001-b:s1e1` |
| Candidate label | `Eneyida` |
| `reason` | `null` |
| Server alignment latency | **15,820 ms** |
| Request middleware time | **16,128 ms** |
| `curl` total | **16.132 s** |

The returned offset is the same story moment 18.6 ms later in B. The
confidence is above the `0.80` match gate; the margin is above `0.05`. The
route's full response, including the resolved B stream URL and headers, was
validated by the request but is not reproduced here.

Backend evidence line:

```text
teleport status=matched item=kinotron:9709-gra-prestolv:s1e1
candidate=eneyida:series/1136-gra-prestoliv-2001-b:s1e1
offset_s=0.018572018159375148 confidence=0.9829963235294118
margin=0.3437500000000001 latency_ms=15820 reason=None
```

## Reproduce

Start the backend with the real tools on `PATH`:

```bash
cd backend
export PATH="$PWD/tools/fpcalc/usr/bin:$PATH"
export LD_LIBRARY_PATH="$PWD/tools/fpcalc/usr/lib"
export CS_UK_TELEPORT_ENABLED=1
.venv/bin/python -m uvicorn cs_uk_api.main:app --host 127.0.0.1 --port 8003
```

Resolve A's stream, extract the clip, then post it. The headers must be the
ones returned by `/api/stream/{item_id}`; the pair ids below are the ones
verified above.

```bash
# $URL and $HEADERS come from GET /api/stream/kinotron:9709-gra-prestolv:s1e1
ffmpeg -hide_banner -headers "$HEADERS" -ss 585 -t 15 -i "$URL" \
  -vn -ac 2 -ar 48000 -c:a pcm_s16le -y /tmp/reference-600s.wav

curl -sS -X POST http://127.0.0.1:8003/api/teleport/align \
  -F 'item_id=kinotron:9709-gra-prestolv:s1e1' \
  -F 'candidate_id=eneyida:series/1136-gra-prestoliv-2001-b:s1e1' \
  -F 'position_ticks=6000000000' \
  -F 'window_seconds=15' \
  -F 'audio=@/tmp/reference-600s.wav;type=audio/wav'
```

## Findings and limits

- The live orchestration works: candidate validation, shared stream
  resolution, HLS decode, Chromaprint alignment, `matched` response, and
  the computed `seek_ticks`.
- The offset is stable with the earlier calibration (`+0.019 s`), which
  used the aligner directly; this run proves the same result through the
  HTTP route.
- The 15.8 s alignment / 16.1 s total is one observation, not a p95. It is
  inside the 45 s server budget and the 20 s latency target in #443.
- The clip was extracted server-side from A, so this does **not** prove
  client decoded-audio capture. It also does not prove that the returned B
  stream plays or seeks on the PS4; those remain the #441 client gate.
- #443 and #441 stay open until the on-device capture/switch acceptance
  passes.

## Re-run 2026-10-27 — merged HEAD, cold path, repo-local tools

**What changed:** the endpoint work was merged (PR #459, plus the seam refactor
in PR #460), the runbook gained the teleport packages/knob, and the opt-in
integration check was driven for the first time. This re-run asks whether the
**merged** route still answers the calibrated pair the same way; it is not a
new acceptance and not the device gate.

### Environment

| Input | Value |
| --- | --- |
| Backend | loopback `127.0.0.1:8015`, run on the tree at the merge of PR #459 (`2cb20ff`, endpoint + refactor #460) |
| Cold start | `CS_UK_CATALOG_WARM=0`, absent snapshot path, isolated resume/user-state paths, no `/api/search` issued |
| Tools | `ffmpeg` 9.0.1; `fpcalc` 1.6.1 from the gitignored `backend/tools/fpcalc/` on `PATH`/`LD_LIBRARY_PATH` |
| Reference / candidate | `serialno:28-gra-prestoliv:s1e1` → `uaserialspro:274-gra-prestoliv-s2011:s1e1` (*Game of Thrones* S1E1, `ТЕТ`) |
| Position | 600 s; clip captured server-side by the script (2,813 KiB posted) |
| Driver | `scripts/teleport_calibrate.py --position 600 --base-url http://127.0.0.1:8015` (cold path, the default) |

### Result

| Metric | Value |
| --- | --- |
| `status` | `matched` |
| `offset_seconds` | **+0.018572** |
| `confidence` | **0.9881** |
| `margin` | **0.3520** |
| `seek_ticks` | 6,000,185,720 (600.0186 s) |
| Server alignment (`latency_ms`) | **13,517 ms** |
| Total request | **14.37 s** |

Backend evidence line (route's own log):

```text
teleport status=matched item=serialno:28-gra-prestoliv:s1e1
candidate=uaserialspro:274-gra-prestoliv-s2011:s1e1
offset_s=0.018572018159375148 confidence=0.9880514705882353
margin=0.35202205882352944 latency_ms=13517 reason=None
```

The playback axis recorded the candidate resolution under the teleport surface
(`/api/health`: `serialno served 1`, `uaserialspro served 2`, no refusals), so
the request that produced this verdict is attributed, not anonymous.

### Checks run alongside

- Opt-in integration, driven for the first time in this tree: with both tools
  present, `test_aligner_recovers_generated_delay_with_real_tools` **runs
  instead of skipping** and recovers the generated 20 s insertion (inside 1 s,
  confidence ≥ 0.80). The check is `skipif`-guarded on `shutil.which`, so a
  host without the tools skips it rather than failing the default suite.
- Gate with both tools on `PATH` at `c6d40ac` (which adds the `/api/health`
  readiness block; the align path is unchanged): pytest **1962 passed,
  1 skipped**, `ruff check cs_uk_api` clean, `mypy cs_uk_api` strict clean
  (103 files), doc-drift guard clean over 10 living docs.

### Limits

- One position on one same-dub pair, cold path, loopback. The verdict matches
  the 2026-10-26 measurements for this pair exactly (0.9881 / 0.3520 /
  +0.0186 s); the pairs differ from the earlier acceptance (kinotron →
  eneyida), so the numbers are comparable, not identical.
- The clip was extracted server-side with `ffmpeg`, not captured from PS4
  decoded audio. No console was attached; the `/Videos/…` facade hop was not
  exercised and no client switch or seek happened. This remains route
  evidence, **not** the #441/#450–#455 device gate.
