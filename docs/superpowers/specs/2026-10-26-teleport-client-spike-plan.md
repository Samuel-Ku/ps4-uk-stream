# Switchfin Teleport Client — Spike Plan

**Date:** 2026-10-26
**Status:** Proposed spike (no client code exists yet)
**Parent:** #441 (Semantic Source Teleport)
**Backend contract:** #443, implemented in `f9c4547` and fixed in `840cb7d`
**Reference client:** `dragonflylee/switchfin` at `0035d6b` (the read-only clone in `backend/tools/switchfin`)
**Target:** PS4 FW 11.00 + GoldHEN, Switchfin PS4 build (mpv 0.36.0)

## Goal

Answer one question with evidence:

> Can the fork capture the last 15 seconds of the **decoded soundtrack it is actually playing**, send it to `/api/teleport/align`, load the returned alternate stream, seek to the returned position, and show an honest fallback — on a real PS4?

This is a spike, not a product ticket. The output is a throwaway branch, a patched-mpv proof, device logs, and a GO/NO-GO. It must not be merged as the feature.

The spike has a hard capture gate at the end of **S1**, before endpoint or UI work:

- **Capture GO:** the fork can hold a clean, anchorable 15-second PCM buffer while playing, with no audible dropout and no second decode of a possibly-dead source.
- **Capture NO-GO:** stop. Do not spend the remaining days on endpoint/UI work and do not substitute a microphone recording or a second network decode. Per #443, a stock client cannot satisfy the success criterion without this tap.

## Why this is the hard part

Stock Switchfin has no audio tap. mpv exposes no decoded-audio callback to libmpv embedders, and its `pcm` AO cannot run alongside the real AO: `--ao` is a priority list with exactly one active driver. mpv's scripting and property APIs expose timing and formats, not samples. A second mpv handle decoding the same URL is not a substitute: the motivating failure is source A being unavailable, so the reference must already be in memory.

The only viable capture point is inside mpv's audio path: just after decoded PCM is handed to the AO. That requires a small patch to libmpv and a rebuild of the PS4 package. This is the spike's primary risk, not an implementation detail.

### Rejected capture alternatives

| Option | Why it fails |
| --- | --- |
| Microphone / TV line-in | Room noise, A/V drift, no clean soundtrack; explicitly forbidden by #443 |
| Second mpv handle / FFmpeg decode of the same URL | The URL may be the thing that just died; second connection also doubles provider load |
| `--ao=pcm` on the playing handle | Replaces the real audio output; no simultaneous playback |
| `--stream-record` / `--record-file` + decode later | Records the compressed stream, not decoded PCM; rolling ring/trimming is fragile; backend contract requires WAV |
| mpv Lua/JS script | No access to sample buffers |
| Platform audio capture | PS4 has no loopback of the title's output; tapping the AO is still required |
| Server fetch of source A | Cannot work when A is dead; rejected in #443 Decision 1 |

The fallback if the libmpv rebuild is unavailable is **no-go**, not a different capture mechanism.

## What already exists

### Backend contract (implemented)

`POST /api/teleport/align`, multipart:

| Field | Type | Rule |
| --- | --- | --- |
| `item_id` | string | current playable id |
| `candidate_id` | string | alternate playable id for the same work and episode |
| `position_ticks` | int | ≥ 0; Jellyfin ticks (10,000,000/s); **the end of the clip** |
| `audio` | file | WAV, 16-bit PCM, 1–2 channels, 10–20 s, ≤ 4 MiB |
| `window_seconds` | float | default 15; 10–20 accepted |

`200` outcomes:

- `matched`: `stream` and `seek_ticks` present; auto-switch is allowed.
- `ambiguous`: `stream` and `seek_ticks` may be present as a **suggestion**; auto-switch is forbidden.
- `no_match`: `stream`/`seek_ticks` are null; `candidates` carries the considered source(s); `reason` is one of `low_confidence`, `reference_quiet`, `decode_failed`, `candidate_no_audio`.

Errors: `400 invalid_request|invalid_audio|audio_too_large|candidate_mismatch`, `404 not_found`, `502 candidate_unavailable`, `503 teleport_unavailable`, `504 alignment_timeout`.

`seek_ticks = max(0, position_ticks + round(offset_seconds * 10_000_000))`. A positive offset means the same moment is later in the alternate source.

### Verified calibration pair

Use the existing real-audio evidence in [`teleport-thresholds-2026-10-26.md`](../../test-artifacts/teleport-thresholds-2026-10-26.md):

- Positive: *Game of Thrones* S1E1, kinotron `ТЕТ` → eneyida `ТЕТ`; matched, confidence 0.9830, margin 0.3438, offset +0.019 s.
- Negative: *Клініка* S1E1, kinotron `Цікава Ідея` → serialno `Paramount Comedy`; `no_match`, confidence 0.5997.
- Negative: *Клініка* S1E1, kinotron `Цікава Ідея` → uaflix `uk`; `ambiguous`, confidence 0.6075.
- Same video, different dub: kinotron `Цікава Ідея` → kinotron `Колодій…`; `ambiguous`, confidence 0.7279, margin 0.1163.

The thresholds remain `0.80 / 0.05 / 0.60`; the spike must not retune them.

### Client seams to use

| Seam | Location | Use |
| --- | --- | --- |
| `MPVCore` singleton | [`mpv_core.hpp`](../../../backend/tools/switchfin/app/include/view/mpv_core.hpp) | one mpv handle, server/stream URL loading |
| `MPVCore::setUrl` / `seek` | [`mpv_core.cpp`](../../../backend/tools/switchfin/app/src/view/mpv_core.cpp) | load stream with `start=`, reset capture on seek |
| `MPVCore` end-file handler | [`mpv_core.cpp`](../../../backend/tools/switchfin/app/src/view/mpv_core.cpp) (`MPV_EVENT_END_FILE`) | already distinguishes `MPV_END_FILE_REASON_ERROR` |
| `VideoView` error dialog | [`video_view.cpp`](../../../backend/tools/switchfin/app/src/view/video_view.cpp) (`MPV_FILE_ERROR`) | hook for the "Try another source" action |
| `PlayerView` playback state | [`player_view.cpp`](../../../backend/tools/switchfin/app/src/activity/player_view.cpp) | `itemId`, `stream`, `playMedia`, `reportStart/Stop/Play` |
| `HTTP` | [`http.hpp`](../../../backend/tools/switchfin/app/include/api/http.hpp) | libcurl wrapper; currently JSON/form only, **no multipart** |
| `AppConfig` | [`config.hpp`](../../../backend/tools/switchfin/app/include/utils/config.hpp) | `getUrl()`, `getToken()`, config directory |

`PlayerView::playMedia` currently POSTs Jellyfin `PlaybackInfo` and then loads a facade `/Videos/.../stream` URL. The teleport path must not reuse that function: the returned alternate is a provider URL + headers, not a Jellyfin media source.

## Spike design

### D1 — Capture point: patched libmpv AO, 20-second ring

Add a small patch to the mpv 0.36 source used by the PS4 package:

- Hook the decoded-PCM copy in `audio/out/buffer.c`'s `read_buffer`, immediately after the `memcpy` loop and **before** `ao_post_process_data`. This is one hook for both push AOs (`ao_play_data`) and pull AOs (`ao_read_data`), and it is before mpv's software gain/volume post-processing: muting or lowering the volume does not attenuate the reference.
- Convert to **interleaved stereo, signed 16-bit, 48 kHz** in the tap. Set `audio-samplerate=48000` with `mpv_set_option_string` in `MPVCore::init` for the spike (PS4 already uses `audio-channels=stereo`). If the AO negotiates another rate anyway, the tap downsamples by an integer factor with a 2-tap average; log the actual rate. Ensure `audio-spdif`/passthrough is off for the spike: a bitstreamed track is not decoded PCM and cannot be tapped.
- Keep a **20-second ring** (3.84 MB / 3.66 MiB at 48 kHz stereo s16) with a mutex around write and snapshot. Audio-thread writes are small (`read_buffer` chunk sizes); snapshots are only taken after pausing/stopping playback, so the lock is not on the steady-state critical path.
- Expose a tiny C API from the patched library and install it with the package:

```c
extern "C" {
void     mpv_teleport_tap_reset(double base_seconds);
size_t   mpv_teleport_tap_copy_last(double seconds, int16_t *dst, size_t dst_frames);
double   mpv_teleport_tap_end_seconds(void);
int      mpv_teleport_tap_rate(void);
int      mpv_teleport_tap_channels(void);
}
```

- The tap is a compile-time build feature (`TELEPORT_TAP`). If the fork must ship one binary, gate it with a runtime check but keep the ring allocation lazy.
- The app wraps the snapshot in a canonical 44-byte RIFF/WAVE header (PCM, 2 channels, 48 kHz, 16-bit). `mpv_teleport_tap_copy_last` returns interleaved frames, so the WAV data size is `frames * 2 * sizeof(int16_t)`.

**Reset rules:** call `mpv_teleport_tap_reset(base)` when a load or seek completes (`MPV_LOADED` / `MPV_EVENT_PLAYBACK_RESTART`), not when the command is merely issued. `base` is the media position of the first sample after the reset (`start=` seconds for a load, the target for a seek). Clear the ring on `setUrl` and `seek` as a belt-and-braces measure; the post-restart reset removes stale pre-seek audio.

**Why not tap after the device?** The device may resample or mix; the shared `read_buffer` copy is the last point where the samples are known to be the decoded soundtrack, and it is before mpv's gain so volume changes do not matter.

### D2 — Anchor: the end of the ring, not `playback_time`

The request's `position_ticks` is the end of the uploaded clip. Use the **tap clock**:

```text
tap_end_seconds = base_seconds + frames_written / 48000
position_ticks   = llround(tap_end_seconds * 10_000_000)
```

Do not use `MPVCore::playback_time` for the request by default. The AO is fed ahead of the speakers (device buffer), so `playback_time` and the last queued sample can differ. The tap clock is internally consistent with the clip's end; the resulting seek is at most one AO buffer later than the last sample the viewer heard (target ≤ 0.5 s error).

Log both values and their delta on every attempt. If the measured delta is consistently above 0.5 s, the next iteration can subtract the measured AO buffer latency or timestamp ring blocks at the `read_buffer` hook; do not guess a constant in this spike.

At failure, mpv has already stopped writing audio; snapshot the ring immediately. For a manual healthy-playback capture, pause first and wait one AO buffer (~100 ms) before reading.

Speed handling: the tap clock counts output samples, which equals media time only at 1.0x. For the spike, attempt teleport only at 1.0x; if the player speed is not 1.0, log `speed` and fall back to the stock error path. Scaling the anchor by a measured tempo ratio is a follow-up, not a guessed constant.

### D3 — Trigger, attempt budget, and state

The spike triggers automatically from the existing playback-error path. Production stall detection is out of scope.

- Extract the current `MPV_FILE_ERROR` dialog into `VideoView::showPlaybackError()` and add `VideoView::setTeleportAction(std::function<void()>)`. In the error branch, if the action is set, call it instead of opening the stock dialog; on any teleport failure, call `showPlaybackError()`.
- `PlayerView` sets the action to `attemptTeleport()`.
- While `Aligning`, show the existing center spinner/label with “Looking for another source…” and a Cancel action. The spinner is bounded by the 60 s client timeout and must not block the UI thread.
- Add a debug-only OSD action or key binding that calls the same method during healthy playback, so capture and endpoint can be tested without killing a provider.
- For the spike, read the candidate id from `teleport-spike.json` in `AppConfig::configDir()`:

```json
{"candidate_id": "kinotron:...:s1e1"}
```

  Production must take it from the fork's existing source list; see S0.

State machine (one automatic attempt per failure):

| State | Entered when | Exit |
| --- | --- | --- |
| `Idle` | normal playback | `MPV_FILE_ERROR` or debug action |
| `Capturing` | trigger | clip + anchor ready, or fallback |
| `Aligning` | request sent | 200, error, timeout, cancel |
| `Offering` | `ambiguous` / `no_match` | user chooses, or dismisses |
| `Switching` | `matched` or confirmed `ambiguous` | `MPV_LOADED` + seek verified |
| `Done` | switch complete or user dismissed | never auto-retries |

Set `attempted = true` before the request. Reset it only when a **normal** (non-teleport) file loads successfully, or when the player view is recreated. If the teleport target itself fails, show the stock error dialog; do not teleport again. If the ring has less than 10 s of audio, or no candidate id is configured, skip the request and show the stock error immediately; an early failure has no usable reference.

Threading and lifetime: snapshot the ring on the main thread after pausing/stopping playback, run the HTTP request on `brls::async`, and deliver the result with `brls::sync`. Keep the `PlayerView` alive with the existing `ASYNC_RETAIN`/`ASYNC_RELEASE` pattern, carry an attempt id so a late response after Cancel/teardown is ignored, and dismiss the spinner before showing either the switch or the stock error dialog.

### D4 — Endpoint client

Add `app/include/api/teleport.hpp` and `app/src/api/teleport.cpp`.

- URL: `AppConfig::instance().getUrl()` with any trailing slash removed, plus `/api/teleport/align`.
- Native route: no Jellyfin token, no `Authorization` header. It is LAN-only and unauthenticated by design (#443).
- Multipart using libcurl's mime API (`curl_mime_init`, `curl_mime_addpart`, `curl_mime_name`, `curl_mime_data`, `curl_mime_filename`). Do **not** use `HTTP::_post`: it sends a raw body and throws away error bodies.
- Add `HTTP::post_multipart(...)` returning `{long status; std::string body;}`. It must not throw on HTTP 4xx/5xx; the client needs `detail.error` to choose a UI path.
- Timeout: 60 s total. The server's 45 s budget covers queue wait + alignment, but candidate validation, stream resolution, and the multipart upload happen around it; a 50 s client timeout could abort a request that the server is about to answer with 504. Support `HTTP::Cancel` so the dialog's Cancel button aborts the curl transfer; the server may still finish its job.
- Request fields: `item_id`, `candidate_id`, `position_ticks`, `window_seconds=15`, `audio` (`teleport.wav`, `audio/wav`).
- Response parsing: `status`, `source_id`, `stream` (optional), `seek_ticks` (optional), `offset_seconds`, `confidence`, `margin`, `candidates[]`, `reason`. Use `nlohmann::json` with `value()` defaults; do not require fields that are null for `no_match`.
- Validate `stream.url` is `http://` or `https://` and non-empty before handing it to mpv. Reject header names/values containing CR/LF. Never log the URL, headers, or audio bytes.
- Parse error bodies as `{"detail":{"error":..., "message":...}}`; fall back to `http status N` when the body is not JSON.

### D5 — Switch and seek

`PlayerView` needs a dedicated path; calling `playMedia` would re-request Jellyfin `PlaybackInfo` for the old item and load the wrong URL.

```text
playTeleport(response):
  reportStop()                       // old session; facade treats it as a no-op
  mpv.stop()
  mpv.setHttpHeaderFields(response.stream.headers)   // [] clears the list
  mpv.setUrl(response.stream.url,
             "network-timeout=...,start=<seek_seconds>",
             "replace")
  itemId = sourceId = response.source_id
  stream = minimal Source{Id=response.source_id, SupportsDirectPlay=true}
  targetSeconds = seek_ticks / 10_000_000
  pendingSeekVerification = true
```

Implementation notes:

- Add `MPVCore::setHttpHeaderFields(const std::map<std::string,std::string>&)`. Set **`file-local-options/http-header-fields`** via `mpv_set_property(..., MPV_FORMAT_NODE, ...)` with an array of strings; this is the same per-file option path mpv's own ytdl hook uses, and it avoids the comma/quote escaping of the `loadfile` options string. If 0.36 does not accept the property before a load, fall back to an escaped `http-header-fields=` entry in the `loadfile` options string. Clear the list before every normal `playMedia` load so provider headers cannot leak into the next item.
- Pass `start=` as decimal seconds (`std::to_string` with 3 decimals) rather than `HH:MM:SS`; `misc::sec2Time` is only second-granular. `start=` seeks before playback, avoiding a visible flash at 0.
- On `MPV_LOADED` (or the first `UPDATE_PROGRESS` after it), compare `mpv.playback_time` with `targetSeconds`. If the error is > 1.0 s, issue **one** `seek(targetSeconds, "absolute")` and log the result. If it is still > 2.0 s, log a failure but leave playback running; the acceptance gate fails.
- `setChapters({}, 0)` and keep the old series/episode list untouched for the spike. Next/previous episode after a cross-provider teleport is out of scope and may point at the old provider.
- The mpv handle already sets global `referrer` and `user-agent` (the Jellyfin server/user agent). A provider header named `Referer` or `User-Agent` must win; verify there is no duplicate conflict. If the globals override the per-file list, clear them for the teleport load and restore them before normal playback.
- The facade's byte routes are public; the returned raw provider URL is loaded directly, with the returned headers. Do not route the teleport stream through `/Videos/...` in the spike: that would add a second resolution and hide whether the returned URL actually works.
- If the raw provider URL fails on PS4 because of TLS/certificate or an expiring token, record it as a contract/deployment finding. Do not silently fall back to a second lookup in the spike.

### D6 — Candidate fallback

No `ambiguous` or `no_match` result may load a stream automatically.

- `ambiguous` with `stream` + `seek_ticks`: dialog with the candidate label and confidence:
  - “Match is not certain (0.61). Switch to <label> at the suggested position?”
  - **Switch anyway** → `playTeleport(response)` (user has explicitly confirmed).
  - **Stay** → stock error dialog.
- `no_match`: dialog listing `candidates`:
  - “No confident match. The other source may be at a different scene.”
  - **Open other source** → the fork's existing source picker, or a new `PlayerView` for the candidate id at the current clock position (spike-only; label it “same clock position, not aligned”).
  - **Cancel** → stock error dialog.
- Network error, `400`, `404`, `502`, `503`, `504`, malformed JSON, unknown `status`, or a `matched` response missing `stream`/`seek_ticks`: stock error dialog. If a source picker is already available, offer it as a secondary action; never auto-switch.
- The response carries at most one candidate in this MVP. Do not invent additional provider discovery in the client.

The stock error dialog stays the fallback of last resort. Teleport must never replace a working error screen with a blank screen or a spinner that cannot be dismissed.

### D7 — Instrumentation

Use one prefix, `TELEPORT`, through `brls::Logger::info`. Log lines must be greppable from logcat and must not contain audio bytes, stream URLs, or header values.

```text
TELEPORT attempt=<id> phase=capture window_s=15.000 frames=<n> bytes=<n>
  anchor_ticks=<n> tap_end_s=<f> playback_s=<f> delta_ms=<n> rate=<n> channels=<n>
TELEPORT attempt=<id> phase=request http=<n> status=<status> confidence=<f>
  margin=<f> offset_s=<f> latency_ms=<n> reason=<r>
TELEPORT attempt=<id> phase=switch requested_s=<f> actual_s=<f> seek_error_ms=<n>
  load_ms=<n> total_ms=<n> headers=<n>
TELEPORT attempt=<id> phase=fallback status=<status> reason=<r> auto=false
```

The backend already logs `status`, `offset_s`, `confidence`, `margin`, `latency_ms`, and `reason`; the client log carries the console-side capture, load, and seek numbers the server cannot see.

### D8 — Safety and resource bounds

- Clip and WAV stay in memory; no audio file is persisted. If the curl implementation requires a temp file, create it under the app temp directory with owner-only permissions and unlink it in a `finally`.
- Cap the clip at 4 MiB before sending. 15 s of 48 kHz stereo s16 is 2.88 MB. If a device negotiates a higher rate/channel count, downmix to mono first, then reduce the window to 10 s and send `window_seconds=10`; log the reduction.
- The ring is 3.84 MB; the WAV copy is ~2.88 MB; libcurl may copy it once. Budget ~10 MB transient, plus the existing player.
- No retry loop. One request per failure, one automatic switch attempt.
- The candidate id comes from the user/UI, never from the response's `source_id` for a new request. No URL field is accepted from the client.

## Work plan

The timing assumes one developer with the PS4 build environment and a device. The Day-1 gate is deliberate.

### S0 — Prerequisites and baseline (0.5 day)

1. Confirm the fork is based on `0035d6b` (or record the divergence); build the stock PS4 package and play one episode.
2. Point the fork at the LAN backend; confirm `/api/teleport/align` answers. Verify `ffmpeg` and `fpcalc` on the backend host (`ffmpeg` + `libchromaprint-tools` on Debian/Ubuntu, `ffmpeg` + `chromaprint` on Arch) and `CS_UK_TELEPORT_ENABLED=1`. The native route is LAN-only; do not expose it to a WAN.
3. Confirm the fork's source list yields **provider-scoped playable episode ids** for the same episode, not only dub translations of the current provider. If it only lists translations, record that as a production prerequisite; the spike uses `teleport-spike.json` with the GoT candidate id.
4. Record the runtime AO and version:

```text
mpv_get_property_string(mpv, "current-ao")
mpv_get_property_string(mpv, "audio-out-params")
mpv_client_api_version()
```

   Expected: PS4 package `ps4-openorbis-libmpv-0.36.0-2`; AO may be SDL or a PS4-specific driver. The tap hook is AO-agnostic, but the test matrix depends on push vs pull.
5. Capture a stock baseline: launch, play 60 s, seek, pause, exit; no crashes, no audio dropouts.

**Exit:** backend reachable, device plays, AO identified, candidate id available (from source list or config file).

### S1 — Capture feasibility (Day 1, hard gate)

1. Obtain the PS4 libmpv source/package recipe used by the build container. Apply the tap patch. Build the package and the PS4 app against it.
2. Verify the tap with a known signal: play a stereo test file, reset the ring, let 20 s pass, dump the last 15 s to a WAV on the host/USB, inspect rate/channels/duration and listen for dropouts.
3. Play the GoT episode; at a known position, snapshot 15 s. Send it to the backend with `candidate_id = item_id` (self-alignment). Expect `matched`, offset ≈ 0.0 s, confidence ≥ 0.80. This proves the captured clip is the same audio the backend decodes from the source.
4. Measure:
   - actual captured duration and bytes;
   - `tap_end_seconds` minus `MPVCore::playback_time` at snapshot;
   - CPU/memory while playing with the ring enabled vs. stock;
   - any audio underrun or frame drop introduced by the tap.
5. Repeat after a seek and after a pause/resume, to prove the ring reset does not mix pre-seek audio.

**Exit (GO):** self-align `matched` at |offset| ≤ 1.0 s, confidence ≥ 0.80; 15.0 ± 0.1 s; ≤ 4 MiB; anchor delta ≤ 0.5 s; no audible dropout; CPU/memory overhead recorded and acceptable on PS4.

**Exit (NO-GO):** no rebuildable libmpv path, or the tap cannot produce clean audio without breaking playback. Stop; write the blocker into the artifact; do not proceed to S2–S5.

### S2 — Endpoint and switch harness (Day 2, desktop first)

1. Add `TeleportClient`, multipart `HTTP::post_multipart`, response structs, and a desktop-only test action. Run on desktop Switchfin against the LAN backend.
2. Use a saved 15 s WAV from S1 and a fixed candidate id. Verify request fields and response parsing for `matched`, `ambiguous`, and `no_match` (use the GoT positive and the *Клініка* negatives).
3. Implement `playTeleport` with headers + `start=`, then verify on desktop:
   - raw provider URL plays;
   - seek lands within 1 s of `seek_ticks` (check `playback_time` after `MPV_LOADED`);
   - a header-gated candidate plays (do not use only the headerless GoT pair);
   - normal playback after a teleport does not inherit provider headers.
4. Inject errors: 400, 404, 502, 503, 504, network timeout. Confirm each maps to the documented UI path and leaves the stock error dialog usable.

**Exit:** desktop matched, ambiguous, no_match, and all error statuses behave as designed. This is a harness result, not the device gate.

### S3 — PS4 endpoint, switch, and seek (Day 2–3)

1. Port S2 to the PS4 build. Keep the debug action available.
2. Play GoT from kinotron at a known position. Trigger teleport (healthy, then with A blocked) to eneyida.
3. Record: capture bytes, request latency, response status/confidence/offset, `loadfile` start, actual `playback_time` after loaded, total switch time, and the audible gap.
4. Repeat with a header-gated candidate and with an HLS candidate if the pair exposes one.

**Exit:** `matched` loads B, `playback_time` lands within 1.0 s of `seek_ticks`, A/V stays in sync, no crash, normal playback still works afterwards.

### S4 — Candidate fallback on PS4 (Day 3)

1. Use a different-dub candidate (`Клініка`): expect `ambiguous`/`no_match`; confirm no stream is loaded automatically and the dialog appears.
2. Confirm the fallback action opens the candidate (or the existing source picker) at a clearly labelled position; cancel returns to the stock error screen.
3. Trigger the same failure twice; confirm only the first teleport attempt is automatic.
4. Simulate backend down / missing tools / timeout; confirm the stock error dialog is unchanged.

**Exit:** no auto-switch on low confidence; dialog is usable with a controller; no retry loop.

### S5 — On-device acceptance and artifact (Day 3–4)

Run the acceptance scenario from #443:

1. Start GoT S1E1 on kinotron, let it play past the intro.
2. Kill source A (block its host or expire the URL) so mpv raises `MPV_FILE_ERROR`.
3. Trigger teleport. The viewer should land in the same scene on eneyida without manually scrubbing.
4. Record the full logcat, the backend `teleport` log line, and a short screen capture or timestamped notes.
5. Repeat once with the *Клініка* different-dub pair; record that it refuses to auto-switch.

Write the evidence into `docs/test-artifacts/teleport-client-2026-10-26.md` using the existing artifact convention. Include the app commit, mpv package version, backend commit, pair, position, confidence, margin, offset, capture bytes, request latency, seek error, and total switch time. Do not include stream URLs, headers, or audio bytes.

## Test matrix

| # | Scenario | Expected |
| --- | --- | --- |
| 1 | Same-dub GoT, healthy A, manual capture | self-align `matched`, offset ≈ 0, confidence ≥ 0.80 |
| 2 | Same-dub GoT, A fails mid-scene | `matched`; switch to B; seek error ≤ 1 s; same scene |
| 3 | Header-gated candidate | raw URL + returned headers plays; no 401/403 |
| 4 | HLS candidate (if available) | loads, seeks, segments fetch; no proxy dependency |
| 5 | Different-dub *Клініка* (serialno) | `no_match`; no auto-switch; dialog |
| 6 | Different-dub *Клініка* (uaflix) | `ambiguous`; suggestion only; confirm required |
| 7 | Same video, different dub (kinotron `Колодій…`) | `ambiguous`; no auto-switch |
| 8 | Wrong episode candidate | 400 `candidate_mismatch`; stock error; no switch |
| 9 | Backend disabled / missing `fpcalc` | 503 `teleport_unavailable`; stock error |
| 10 | Backend slow beyond 45 s | 504; client timeout; stock error |
| 11 | Backend unreachable | curl error; stock error; no crash |
| 12 | Oversized/invalid clip (desktop) | 400 `audio_too_large` / `invalid_audio`; no crash |
| 13 | Seek or pause before trigger | ring resets; no pre-seek audio in clip; self-align clean |
| 14 | Teleport target also fails | stock error; no second automatic attempt |
| 15 | Normal playback after teleport | no leaked provider headers; facade stream still plays |

## Acceptance criteria

The spike is GO only if all of these hold:

**Capture**
- 15.0 ± 0.1 s of 16-bit PCM, 1–2 channels, ≤ 4 MiB.
- Self-alignment against the same source returns `matched`, |offset| ≤ 1.0 s, confidence ≥ 0.80.
- Tap end vs. `playback_time` ≤ 0.5 s at snapshot; the value is recorded.
- No audible gap, underrun, or A/V drift introduced by the tap during 10 minutes of playback.
- Ring reset after a seek/pause produces a clip with no stale pre-seek audio.

**Endpoint**
- One request per attempt; multipart fields and WAV accepted by the real route.
- `matched`, `ambiguous`, `no_match`, and every documented error map to one UI path.
- Client timeout > server budget; cancel is possible; no retries.

**Switch**
- The returned URL + headers load on the PS4 without a facade hop.
- `playback_time` after load is within 1.0 s of `seek_ticks`; at most one corrective seek.
- The switch completes within 5 s after the response (excluding the 8–17 s alignment).
- Playback reporting does not break; normal playback afterwards is unaffected.

**Fallback**
- `ambiguous` and `no_match` never auto-switch.
- The dialog is controller-navigable and cancel returns to the stock error screen.
- A second failure of the teleport target does not auto-retry.

**Overall**
- The GoT scenario takes a real failing stream from A to the same scene in B without a manual seek, with the log artifact showing the numbers above.

Any failure is reported as a failure; a green desktop harness is not an on-device pass.

## Risks and unknowns

| Risk | Impact | Mitigation / decision |
| --- | --- | --- |
| PS4 libmpv package cannot be rebuilt with the tap | Blocks the whole feature | S1 is a hard gate; record NO-GO rather than substituting |
| AO format is planar/float/multichannel | Tap converter bugs, clipped audio | Convert to s16 stereo; force `audio-samplerate=48000`; test with a test file and real episode |
| AO feeds ahead of playback | Anchor error, wrong scene | Use tap clock; log delta; acceptance ≤ 0.5 s; next iteration can model device latency |
| Ring snapshot races with audio thread | Corrupt clip | Snapshot only after error/pause; mutex; 20 s ring with 5 s headroom |
| Raw provider URL fails on PS4 (TLS/cert/token) | Switch fails even though alignment succeeded | Test a header-gated candidate; record as contract finding; do not silently re-resolve |
| Provider URL expires during 8–17 s alignment | Switch 403/404 | Load immediately after response; one attempt; fallback dialog; no retry |
| Fork lacks a cross-provider source list | No production candidate id | S0 verifies; spike uses config file; production ticket follows |
| Header options leak into later playback | Wrong headers on normal items | Clear `http-header-fields` at the start of every normal `playMedia`; test row 15 |
| 4 MiB cap with high-rate audio | 400 `audio_too_large` | Downmix to mono, then 10 s window; log reduction |
| `MPV_FILE_ERROR` only catches hard errors, not stalls | Some failures never trigger teleport | Spike uses the error dialog/debug action; stall watchdog is out of scope |
| PS4 memory/CPU overhead | Audio dropouts, OOM | 3.84 MB ring, ~2.88 MB WAV, ~10 MB transient; measure in S1 |
| Playback reports after candidate id | Facade errors or stale session | Reports are no-ops by design; verify in S3; suppress if needed |
| Translation strings | Dialog untranslated in spike | Hardcode English for the spike; add i18n only if the design is kept |

## Out of scope

- Automatic stall detection or a background health monitor.
- Candidate discovery, cross-provider episode mapping, or multi-candidate ranking.
- Movies, torrent-lane sources, live HLS, DRM, and cross-dub matching.
- Seamless/gapless switching or hiding the interruption.
- Persisting audio, alignment history, or a session registry.
- A sidecar service, new database, or alignment cache.
- Reworking the stock error UI or adding a general source-picker redesign.

## Deliverables

1. A `teleport-spike` branch in the fork (not merged) with:
   - `mpv-teleport-tap.patch` and the PS4 package recipe/version pin;
   - `TeleportClient` + multipart helper;
   - `MPVCore` tap reset and header-list support;
   - `PlayerView` switch/seek path;
   - `VideoView` error-dialog hook;
   - spike config loader and debug action.
2. `docs/test-artifacts/teleport-client-2026-10-26.md` with the device evidence and the GO/NO-GO.
3. A short decision note on #441:
   - GO: link the fork ticket(s) for the tap/package, candidate list, and production UI; keep #441 open until the on-device success criterion is demonstrated.
   - NO-GO: name the exact blocker (libmpv rebuild, audio quality, or anchor error); do not close #441 or mark #443 done.
4. No backend changes. If the spike discovers a contract problem (for example, raw provider URLs are not playable on PS4), document it and open a separate backend ticket; do not hand-edit the deployed endpoint in this spike.

## Open questions to answer during the spike

1. Which AO does the PS4 package actually use, and is it push or pull?
2. Can the existing pacbrew build container rebuild libmpv 0.36 with the tap and produce an installable package without changing the app's link ABI?
3. Does the fork's source list expose provider-scoped episode ids, and does it carry the dub label needed to pick a same-dub candidate?
4. Are the returned raw provider URLs directly playable on PS4 with the returned headers (TLS, redirects, HLS), or does the client need a facade URL?
5. What is the measured difference between the tap end and `playback_time` on the PS4?
6. How long does an 8–17 s backend alignment plus stream load feel on the console, and does the loading indicator remain responsive?

## Tracker state

#441 stays open. #443 remains the backend contract and is not complete until this client gate passes. The spike does not close either issue; it produces the evidence needed to decide whether the client half is feasible.
