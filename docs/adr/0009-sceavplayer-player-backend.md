# ADR-0009: SceAvPlayer as a player backend (gated by env var)

**Status:** proposed
**Date:** 2026-08-02
**Refs:** #36, #65

## Context

The PS4 has fixed-function hardware blocks for AVC (H.264) and HEVC
(H.265). SceAvPlayer (Sony's libavplayer in the PS4 SDK) is the only
public path to the hardware decoder. mpv in pPlay decodes in software
on the Jaguar CPU and chews through it at 1080p.

The spike ticket #36 (closed by design-side research) showed Path A
(SceAvPlayer as a player backend) is viable conditional on three
on-HW facts. The catalog layer already abstracts the player — the
hand-off is `Player::load(MediaFile)` with per-stream HTTP headers
applied via `set http-header-fields`.

This ADR records the integration plan so the spike's results land as
a clean, gated change.

## Decision

Add a second `Player` implementation, `PlayerSceAvPlayer`, alongside
the existing `PlayerMpv`. The selection is keyed by an env var:

```
CS_UK_PLAYER=mpv          # default — safe, current behavior
CS_UK_PLAYER=sceavplayer  # experimental — requires on-HW verification
```

The factory in `Main::getPlayer()` consults the env var and returns
the matching `Player*`. The catalog layer (`ScreenContent::playEpisode`
→ `applyMpvHeaders` → `player->load(MF)`) is unchanged.

## Stream-source adapter

SceAvPlayer does not take a URL directly. It uses `SceAvPlayerSource`
+ `SceAvPlayerStreamAdd`. We implement a small in-process HTTP client
that:

1. Resolves the URL (with the catalog's per-stream headers — Referer,
   User-Agent, Cookie).
2. Reads the master m3u8, picks the bandwidth ladder variant, reads
   the media m3u8.
3. Feeds the media segments back to SceAvPlayer via
   `SceAvPlayerStreamAdd`.
4. Reports timeline / position events via the existing
   `PositionSaver` callback that ScreenContent registered on the
   Player.

The HTTP client reuses the same headers as the catalog's mpv path
(Referer / User-Agent / Cookie) so provider gating continues to
work.

## Fallback policy

`CS_UK_PLAYER=mpv` is the documented default. The SceAvPlayer
backend is opt-in. The spawn check is:

- If `./run-in-ps4-container.sh` finds the SceAvPlayer SDK libraries
  in the OpenOrbis toolchain, the env var `CS_UK_PLAYER=sceavplayer`
  is allowed.
- Otherwise, the factory logs a warning and falls back to mpv.

This means the mpv path stays the production path until the on-HW
spike lands a verified build.

## Out of scope

- AV1 / VP9 / VVC — not supported by the PS4 hardware at all.
- Demux of MP4 / WebM container media — SceAvPlayer's strengths
  are HLS and MP4 file playback; we keep the mpv path for any
  non-HLS source that surfaces later.
- subtitle / OSD rework — the catalog layer's `PositionSaver` is
  the only contract we depend on; the OSD is rendered by pPlay's
  existing chromium-on-PS4 overlay, not by the player.

## Tests

- [ ] Unit test: factory consults env var and returns the
      matching Player*. The test does not invoke the player.
- [ ] On-HW smoke: a known HLS stream plays through PlayerSceAvPlayer
      with smooth frames (verified by the user on real PS4).
- [ ] On-HW smoke: positionSaver emits `(positionSec, durationSec)`
      callbacks that round-trip through CatalogState.

## Migration

No migration. The default stays `CS_UK_PLAYER=mpv`. Opt-in is
documented in `docs/ps4-test-report.md` and tracked by #65.
