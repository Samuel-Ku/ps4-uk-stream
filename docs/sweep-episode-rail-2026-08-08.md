# Episode-rail verification sweep (2026-08-08)

Diagnostic-only sweep (issue #136): walk the series episode-rail path the
PS4 client actually follows, for every registered provider, and record
which hop breaks. No adapter or facade fixes in this ticket — this is the
map the fix ticket (blocked by #136) targets.

**Path per series:** `/Shows/{g1}/Seasons` → pick a season →
`/Shows/{g1}/Episodes?seasonId={season}` → pick first episode →
`POST /Items/{ep}/PlaybackInfo` → `GET /Videos/{ep}/stream?static=true`.
Each hop must return 200. The first hop that fails is recorded with its
status + error.

**Why a sweep, not the movie gate:** a g1-key `PlaybackInfo` 404 for a
*series* is EXPECTED (D3 — series keys are not playable; the client drills
down through episodes). So a movie-only gate passes even when the series
episode-rail is broken. This sweep targets the series path.

**The empty-200 hazard (the whole point):** `/Shows/{g1}/Episodes`
WITHOUT a `seasonId` returns 200 with an empty `Items` list — the route
defers to `_hierarchy(None)`, a tolerant empty answer (D5). A naive sweep
hitting `Episodes` with just the g1 key would report a false ✅ for EVERY
provider while testing nothing. This sweep therefore goes through
`Seasons` first and only counts the rail as real when a resolved
`seasonId` yields a non-empty episode list. A rail that resolves but
exposes zero episodes is recorded as ⚠️ `no_episodes` (a real break,
BUG-2 / anitubeinua style), not a pass.

**Environment:** live upstream network from the sandbox; uvicorn on
`127.0.0.1:8002`; facade token `jellyfin-dev-token`; `per_provider=3`.
Tool: `backend/cs_uk_api/scripts/sweep_episode_rail.sh` (boots the server,
runs `python -m cs_uk_api.sweep_episode_rail http ...`).

Legend: ✅ all tested series reach stream · ⚠️ rail resolves but exposes no
episodes · 🐛 a hop failed (failing hop + error in notes) · ⏭️ skipped
(zero series for this provider in the warm `/api/home` snapshot).

| provider | series tested | series ✅ | 🐛 | ⚠️ no-eps | verdict | notes |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| uakino | 2 | 2 | 0 | 0 | ✅ |  |
| ufdub | 3 | 3 | 0 | 0 | ✅ |  |
| unimay | 1 | 1 | 0 | 0 | ✅ |  |
| kinotron | — | — | — | — | ⏭️ skip | no series in home snapshot |
| cikavaideya | 2 | 0 | 1 | 1 | 🐛 | g1:16cd3e36c59d5348:PlaybackInfo {"detail":"item_unavailable"} |
| hentaiukr | 2 | 2 | 0 | 0 | ✅ |  |
| bambooua | 3 | 1 | 0 | 2 | ⚠️ | episode-rail resolves but exposes no episodes |
| kinovezha | — | — | — | — | ⏭️ skip | no series in home snapshot |
| animeua | 3 | 2 | 0 | 1 | ⚠️ | episode-rail resolves but exposes no episodes |
| uaflix | 3 | 2 | 0 | 1 | ⚠️ | episode-rail resolves but exposes no episodes |
| coaninet | 3 | 3 | 0 | 0 | ✅ |  |
| eneyida | — | — | — | — | ⏭️ skip | no series in home snapshot |
| klontv | 2 | 2 | 0 | 0 | ✅ |  |
| serialno | 2 | 2 | 0 | 0 | ✅ |  |
| doramyworld | — | — | — | — | ⏭️ skip | no series in home snapshot |
| uaserialspro | 3 | 3 | 0 | 0 | ✅ |  |
| anitubeinua | — | — | — | — | ⏭️ skip | no series in home snapshot |
| simpsonsuatv | 3 | 3 | 0 | 0 | ✅ |  |
| animeon | 3 | 2 | 0 | 1 | ⚠️ | episode-rail resolves but exposes no episodes |

## Coverage

- **19 / 19 registered providers accounted for** (14 swept, 5 skipped).
- **Skipped** — `kinotron`, `kinovezha`, `eneyida`, `doramyworld`,
  `anitubeinua`: no series surfaced in the warm `/api/home` snapshot this
  run (home row cardinality fluctuates with live listings). Per the
  acceptance criteria a provider is skipped only when it has zero series
  in the snapshot; re-run when those rows are populated to fill the gaps.
- **Per provider:** 3 series attempted where available, fewer when the
  snapshot exposed fewer.

## Findings for the fix ticket

- **🔴 cikavaideya** — one series fails at `PlaybackInfo` with
  `item_unavailable` (the single-episode series from the prior diagnostics
  report; upstream sometimes returns a content page with no playable
  stream). One further series exposes no episodes (⚠️). Target: the
  `stream()` / content-resolution path for single-episode titles.
- **🟡 bambooua / animeua / uaflix / animeon** — rail resolves but one or
  two series in three expose **zero episodes** (⚠️ `no_episodes`). Matches
  the prior report's "✅ N eps" note being per-title: some series listing
  shapes don't yield a drill-down episode list. Confirm whether the missing
  episodes are gated (subscription) or a parse gap in `content()`.
- **✅ green** — `uakino`, `ufdub`, `unimay`, `hentaiukr`, `coaninet`,
  `klontv`, `serialno`, `uaserialspro`, `simpsonsuatv`: all tested series
  reach the stream hop (200). Note `hentaiukr` stream still 200s (hevc
  soft-decode risk on PS4 is a separate playback concern, out of scope
  here).

## Re-run

```bash
cd backend && PORT=8002 CS_UK_JF_TOKEN=jellyfin-dev-token \
  ./cs_uk_api/scripts/sweep_episode_rail.sh
```

Writes `docs/sweep-episode-rail-<date>.md`. Logic lives in
`backend/cs_uk_api/sweep_episode_rail.py` (pure, unit-tested under
`cs_uk_api/tests/test_sweep_episode_rail.py`); the bash wrapper only boots
the server and invokes it.
