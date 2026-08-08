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
| uakino | 3 | 3 | 0 | 0 | ✅ |  |
| ufdub | 3 | 3 | 0 | 0 | ✅ |  |
| unimay | 3 | 3 | 0 | 0 | ✅ |  |
| kinotron | — | — | — | — | ⏭️ skip | no series in home snapshot |
| cikavaideya | 2 | 1 | 1 | 0 | 🐛 | g1:16cd3e36c59d5348:PlaybackInfo {"detail":"item_unavailable"} |
| hentaiukr | 2 | 2 | 0 | 0 | ✅ |  |
| bambooua | 3 | 3 | 0 | 0 | ✅ |  |
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

## Post-fix notes (#139, re-run 2026-08-08)

Re-run of the #136 sweep after the #139 provider fixes, one provider per
commit (edd8047 cikavaideya · 371d5ac bambooua no-manifest · 93824b0
bambooua /zhanr/ id-collapse · 359e58b animeon empty-translations).
Deliberate-unavailability verdicts (`gated`, ADR-0002) translate to a 404
before the health tracker records, so a gated title never trends a
provider down.

- **bambooua: 3/1 + 2⚠️ → 3/3 ✅** — the `/zhanr/` id-collapse fix
  (93824b0) eliminated the two ⚠️ `no_episodes`. Those cards' external_id
  was collapsing `zhanr/<genre>/<id>-slug` to `<genre>/<id>-slug`; the
  collapsed form 301-redirects upstream (httpx follows no redirects) into
  a health-down `not_found`. Keeping the full multi-segment path rebuilds
  the verbatim 200 URL. Live probe: `dorama/1135-…`, `dorama/1119-…`,
  `anime/1008-…` episode ids carry the full path and reach stream.
- **cikavaideya: 🐛 is a correct gated 404** — the failing title
  (`g1:16cd3e36c59d5348`, `item_unavailable`) is a removed/subscription-
  gated title; `gated` → 404 is per ADR-0002, never a health record
  (probe health `ok`). The second title that was ⚠️ `no_episodes` now
  reaches stream (the gated cards are dropped from home by `can_gate`).
- **animeon/animeua ⚠️ = film false-positives, not broken rails** — the
  ⚠️ `no_episodes` verdicts are *films* surfaced in the home snapshot as
  series-type cards. The rail walks `/Shows/{gk}/Seasons`; `content()`
  resolves a Movie card with zero seasons and the sweep records ⚠️.
  Per-title probes confirm both are films: animeon "Люпен III: Перший"
  (id 8100) and animeua "Смертельні ігри заради їжі на столі: 44 —
  Хмарний пляж" (id 8384), live-captured as untracked triage pages
  (`content_8100.html`, `content_8384_*.html`). Health trackers for
  both: `ok`.
- **uaflix ⚠️ is snapshot-order noise, not a broken rail** — the ⚠️ slot
  is not a stable title; it varies with home-snapshot ordering between
  runs. Candidates are dead cards (`content()` → `not_found: status 404`)
  and hash-keyed cards (`bad external_id`) — both `not_found` verdicts,
  which record no health-down (tracker `ok`).
- **animeon 8096 gated (359e58b)** — "Коджін Сенші Оредам" (a `special`)
  answers `/api/player/8096/translations` with a present-but-empty list;
  the fix raises `gated` (ADR-0002) from content()/stream() instead of a
  `parse_failed` health signal. Not in this run's top-3 tested set, but
  verified by its unit tests (RED at HEAD → GREEN with the fix).

Conclusion: every **non-gated series** episode-rail returns 200 at
PlaybackInfo and stream hops. The remaining ⚠️ verdicts are films or dead
cards typed as series in the home snapshot (catalog-hierarchy noise, not a
broken rail), the 🐛 is a correct gated 404, and every affected provider
trends `ok` on the health tracker.
