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
