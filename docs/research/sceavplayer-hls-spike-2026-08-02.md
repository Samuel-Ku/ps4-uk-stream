# SceAvPlayer HLS hardware-decode spike — design notes (2026-08-02)

Ticket: [Spike: PoC — HLS + SceAvPlayer (hardware decode) on PS4 (#36)](https://github.com/Samuel-Ku/ps4-uk-stream/issues/36).

The actual on-hardware verification step requires a PS4 (FW 11.00 +
GoldHEN) and a known H.264 HLS stream. Both are outside the agent
host. This document is the **design-side** of the spike — what the
integration would look like, what is known about SceAvPlayer from
public sources, and what the verdict is *conditional* on the on-HW
verification step.

## Why this matters

mpv in pPlay decodes video in software on the PS4 CPU (Jaguar). The
PS4 has fixed-function hardware blocks for AVC (H.264) and HEVC
(H.265) only. AV1/VP9/VVC have no hardware block and are not
real-time in software. SceAvPlayer (Sony's libavplayer in the PS4
SDK) is the official path to the hardware decoder for HLS and
local-file playback.

Public reference implementations of SceAvPlayer-based homebrew
players:

- **PS4-IPTV-Player** — proves HLS + SceAvPlayer works for live IPTV
  channels on FW 11.00. Uses SceAvPlayerInit / SceAvPlayerSourceOpen
  / SceAvPlayerStreamAdd / SceAvPlayerStart.
- **MediaPlayer4PS4** — uses SceAvPlayer for local files. Shows the
  file-source flow.

## What we already know from the existing build (no HW needed)

1. **The PKG pipeline builds and the SELF runs** (Task 19, #32
   closed). The PKG is `PPLA00001.pkg`, magic `\x7FCNT`, paid
   `0x3800000000000011`. create-fself with the fake paid works.
2. **ffmpeg-ps4 + OpenSSL works** (#35 closed). HTTPS / TLS are
   `= 1` in `config_components.h`. So the network stack can reach
   HLS origins via https.
3. **mpv player path exists inside pPlay** (the upstream fork). The
   hand-off is `Player::load(MediaFile)` with a URL and per-stream
   HTTP headers applied via `set http-header-fields`.
4. **The catalog layer already abstracts the player** (ScreenContent
   calls `streamAsync` → `applyMpvHeaders` → `player->load(MF)`).
   This is the only seam we'd touch for SceAvPlayer.

## Two integration paths

### Path A — SceAvPlayer as a player backend (hardware decode)

Add a new `Player` implementation backed by SceAvPlayer. The current
MP4 demux in pPlay's mpv-based path is replaced by SceAvPlayer's
native HLS demuxer. The MP4 / WebM demux path collapses to "HLS
only" for the SceAvPlayer backend, since SceAvPlayer's HLS is
mature and its MP4 is not.

**What mpv features would be lost:**
- Per-stream subtitle tracks (SceAvPlayer has subtitle support;
  the integration cost is medium, not zero).
- The `osd-show` / `osd-message` cycle for buffering / seeking.
- mpv's filter chain (crop, scale, sharpen) — we never used this
  for HLS, so the loss is silent.
- mpv's `set http-header-fields` direct forwarding — SceAvPlayer
  uses `SceAvPlayerSourceOpen` with a wrapper stream source, so we
  implement a tiny in-process HTTP client that injects the
  catalog's per-stream headers and feeds the bytes back through
  `SceAvPlayerStreamAdd`.

**What is gained:**
- Hardware H.264/HEVC decode → low CPU, smooth 1080p playback.
- HLS demux is SceAvPlayer's bread and butter; we no longer need
  an HLS demuxer in userspace.

### Path B — keep mpv, replace the codec plugin

mpv in pPlay is built against a custom ffmpeg. The ffmpeg build
already targets PS4. The remaining gap is that the AVC/HEVC
hardware decoder is not exposed to ffmpeg: the PS4 SDK keeps the
decoders behind `SceAvPlayer`. There is no public path to use
`SceAvPlayer` as a hardware backend for ffmpeg's libavcodec — the
two are independent libraries.

**Verdict: Path B is not viable.** The hardware decoder is gated
behind SceAvPlayer; ffmpeg cannot consume it. Path A is the only
hardware-decode path.

## Verdict (conditional on the on-HW verification)

If the on-HW step (out of band for this agent) confirms the
following three facts, Path A is viable:

1. **SceAvPlayer HLS demux works for non-IPTV HLS** — PS4-IPTV-Player
   uses SceAvPlayer for live channels; whether it works for the
   catalog's VOD HLS (ref= master m3u8 with bandwidth ladders) is
   not known.
2. **CPU usage drops** — the spike has to demonstrate a
   software-mpv → SceAvPlayer CPU drop on the same stream. Without
   this, the integration cost is not justified.
3. **No regression on the existing mpv path** — the player hand-off
   is already abstracted; the integration keeps the mpv path as a
   fallback (env var `CS_UK_PLAYER=sceavplayer|mpv`), and the
   fallback is the documented default.

**Rough integration effort: Medium.** A spike ports `Player::load`
into two backends; the catalog layer changes are minimal (one
factory switch keyed by env var). The resolution to evaluate is
saved for #65, which is correctly blocked by this ticket.

## What the agent has done vs. what is left

- [x] Researched public SceAvPlayer usage in PS4-IPTV-Player and
      MediaPlayer4PS4 (read-only, no HW).
- [x] Audited the existing mpv integration to find the abstraction
      seam (Player::load + per-stream headers).
- [x] Confirmed the ffmpeg-ps4 + OpenSSL pipeline can reach the
      HLS origin via https (#35).
- [x] Wrote the verdict + Path A/Path B / fallback design.
- [ ] On-HW verification: build a small PS4 homebrew that calls
      SceAvPlayer for a known HLS stream, capture CPU usage.
- [ ] Integration: implement PlayerSceAvPlayer alongside PlayerMpv.
- [ ] Gate the default: `CS_UK_PLAYER=mpv` (the safe path) until
      Path A is verified end-to-end.

Refs #36, #65.
