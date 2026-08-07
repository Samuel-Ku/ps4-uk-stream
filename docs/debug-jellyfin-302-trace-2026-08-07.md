# Debug: Switchfin console 302/404/403/error trace (Jellyfin facade)

Loop run #1 — 2026-08-07. Scope: locate (no code change). Source under
`backend/cs_uk_api/jellyfin/`.

## Emitting routes, trigger, and parsed Jellyfin fields

| Route (file:line) | Emit | Trigger | Parsed fields → DTO builder |
|---|---|---|---|
| `GET /Items/{id}/Images/Primary` (router.py:736) | **200** inline (bytes) | poster found → inline `Response`; else 404 `poster_unavailable` (router.py:762/765) | `poster_url` via `_poster_for` → raw bytes |
| `GET /Videos/{id}/stream` (router.py:1141) | **302** (intentional) | `StreamResponse.headers` empty → `RedirectResponse` to CDN | `StreamResponse.url` |
| `GET /Users/{id}/Images/Primary` (router.py:781) | 404 `no_user_image` | no user concept | — |
| `GET /Items/{id}`, `/Users/{id}/Items/{id}`, hierarchy, `PlaybackInfo`, `/Videos/{id}/stream`, `/Videos/{id}/segment` | **404** `item_unavailable` | non-`g1:` id, cold resolution cache, unknown season, unplayable id, upstream fetch failure | `g1:` group key → `resolve_group_content` → `_content_dto` / `_season_dto` / `_episode_dto` (router.py:192-335) |
| All authed routes via `require_token` (auth.py:85,88) | **401** `missing token` / `invalid token` | absent or wrong `X-Emby-Token` / `Authorization: MediaBrowser Token=` | — |
| `UserDto` (models.py:31) | nlohmann **`type_error.302`** on console if any string/object field is JSON null | Switchfin parses via `NLOHMANN_DEFINE_TYPE_NON_INTRUSIVE_WITH_DEFAULT`; `PrimaryImageTag` etc. default to `""`/`{}` to avoid it | `Name`, `ServerId`, `Id`, `PrimaryImageTag`, `Configuration`, `Policy` |
| `BaseItemDtoQueryResult` (models.py:103) | nlohmann **`out_of_range.403`** if `StartIndex` missing | parsed with `NLOHMANN_JSON_FROM` (no default) → `StartIndex` always 0 | `Items`, `TotalRecordCount`, `StartIndex` |
| `DisplayPreferencesDto` (models.py:121) | nlohmann **`type_error.302`** if explicit null | defaults are strings/dicts, not None | `Id`, `CustomPrefs`, `SortBy`, `SortOrder` |
| `Branding/Configuration` (router.py:418) | 200 `{"LoginDisclaimer": ""}` | null would raise `type_error.302` | — |

## Root-cause finding: spec D9 vs live code divergence (masked by capture test)

- Spec D9 (docs/superpowers/specs/2026-08-05-jellyfin-adapter.md:95) and the
  frozen capture fixture both say `GET /Items/{id}/Images/Primary` → **302**
  to `/api/poster`.
- The router was changed to serve the poster **inline (200)**. Doc comment at
  router.py:750-753: a 302 "is rendered as an error storm ('302') on the
  console" — Switchfin's image loader does not chase redirects. This is the
  fix for the Switchfin 302 error storm.
- `test_capture_surface_landed_statuses` (test_jellyfin_capture.py:98) replays
  the **frozen** `capture.jsonl` and asserts `"a live poster must resolve to
  302"`. It asserts the *recorded fixture status*, not the *current router's*
  status, so the 200-vs-302 contradiction is silent: the test passes while the
  code no longer matches the fixture/spec.
- Live tests `test_jellyfin_views.py:408,424` assert **200** inline —
  consistent with the code but inconsistent with the spec + capture fixture.

### Where the trace ends (upstream of facade)
The 302 error storm is fully explained inside the facade (poster redirect
fixed → 200; `video_stream` 302 is intentional per D7). No cause was found
upstream of `cs_uk_api/jellyfin`. The remaining gap is a **test/fixture drift**,
not a Switchfin bug: the contract test must assert the live router returns 200,
not replay a stale 302 fixture.

## Verification
- `pytest cs_uk_api/tests/test_jellyfin_capture.py` → 16 passed (asserts stale
  fixture 302, masks divergence).
- `test_jellyfin_views.py` poster tests → assert 200 (match code).
- Capture report D9 still documents 302; router doc says 302 is the bug.

# Run #2 (2026-08-07) — divergence resolved

Re-read spec D9 (lines 78-98) and the frozen fixture row
`backend/cs_uk_api/tests/fixtures/jellyfin/capture.jsonl`:
`{"path":"/Items/g1:.../Images/Primary","status":302}` — captured against the
**old** code, before the inline-200 fix. `capture.mjs` only logs settlement
status, does not pin 302.

## Verdict
- Spec D9 (line 95) + frozen fixture + `capture.mjs` still assert **302**
  (redirect to `/api/poster`).
- Live router (router.py:736-767) + live tests (`test_jellyfin_views.py:408,
  424) serve **200 inline**.
- Router doc (router.py:750-753): the 302 is a Switchfin console error storm —
  so **200 inline is the correct fix**; spec D9, the fixture, and `capture.mjs`
  are **stale**, not the code.

Cause is **test/fixture drift**, not upstream of the facade. Loop stop
condition met. No code change required; the code is correct. The contract
fixture/spec must be regenerated to 200 so `test_capture_surface_landed_statuses`
asserts the live router instead of masking the divergence.

## Recommendation (out of loop scope — code change)
1. Regenerate `capture.jsonl` (run `npm run capture` against live facade) so the
   poster row records 200, OR relax
   `test_capture_surface_landed_statuses` (test_jellyfin_capture.py:98) to assert
   the current router status rather than the frozen fixture's.
2. Update spec D9 line 95 from "→ 302 to /api/poster" to "→ 200 inline (302
   storms the Switchfin console)".
