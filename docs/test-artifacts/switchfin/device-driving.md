# Driving the real Switchfin Android client over adb (input + observation)

**Status:** learned during the #152 real-device run (2026-08-10). This is the
operational knowledge for running `scripts/switchfin_test.py` against a real
device — what works, what does not, and why. Keep it up to date when you
discover something new; the opposite (rediscovering this) costs hours.

## Device + app setup

- Phone: OnePlus 8 Pro (IN2023), Android 14, **no root** (`sendevent` → Permission denied).
- Connect over wifi adb:
  `adb connect 192.168.2.143:38631` (address changes — re-check with the owner).
- Screen: physically **landscape**, display 3168×1440 (`ROTATION_90`); the
  touch panel is **portrait-ranged** (ABS 0–1438 × 0–3167).
- App: `fun.dragonfly.switchfin`, version **0.9.3** (versionCode 2183), a
  Qt/SDL (borealis) client — renders its own UI, so **uiautomator dump is
  useless** (no accessibility tree: 6 empty nodes, no text).
- App config (`/sdcard/Android/data/fun.dragonfly.switchfin/files/config.json`):
  server `http://192.168.2.166:8003`, stored session (user `g`, token
  `jellyfin-dev-token`, server id `8e825b6f9a93a6ec`).
  → **The backend must run on port 8003** (not the runner default 8000) or the
    app will not connect. Run it exactly like `cold_start()` in the runner
    does, with `CS_UK_JF_CAPTURE_DIR` set.

## Observing the screen

- `adb exec-out screencap -p` **corrupts the PNG over wifi adb** (truncated
  IDAT). Use `adb shell screencap -p /sdcard/phone.png` then `adb pull`.
- There is no vision available to the agent reliably; use:
  - `tesseract phone.png stdout -l ukr+eng --psm 11 tsv` for text + bounds;
  - PIL pixel analysis for icon/band geometry (sidebar icons, card columns);
  - a local HTML file with the PNG as a **data-URI JPEG** (relative `img`
    paths are NOT served by the preview) to eyeball the screen live:
    `python3 .scratch-phone-preview/shot.py [--tap X Y --label "..."]`
    draws a red marker at the tap point and writes `phone.html`.
- The app logs nothing useful to logcat (Qt logs to its own stream).

## Input — the critical part

| Method | Works? | Notes |
|---|---|---|
| `input keyevent KEYCODE_DPAD_* / CENTER / BACK` | ✅ | Controller-style navigation works fine |
| `input tap X Y` | ❌ | **Too fast.** The app polls `SDL_GetNumTouchFingers` once per frame; a DOWN+UP in ~50 ms is missed entirely between polls → no click |
| `input touchscreen swipe X Y X Y 350` | ✅ | **The working tap.** A press held ≥ ~300 ms registers as a click. This is what the runner must use |
| `input mouse tap X Y` | ⚠️ | SDL does handle mouse events (`onNativeMouse`), but unreliable here — prefer the hold-tap |
| `sendevent` | ❌ | No root |
| `input motionevent DOWN/UP` | ❌ | Same too-fast problem |

### Why (root cause, from source)

`library/lib/platforms/sdl/sdl_input.cpp` (`updateTouchStates`) polls
`SDL_GetNumTouchFingers()`/`SDL_GetTouchFinger()` **once per frame**; there is
no event queue for touches. Mouse buttons go through `SDL_AddEventWatch`
(event-driven), which is why the mouse path works. `SDL_HINT_TOUCH_MOUSE_EVENTS`
is set to `"0"` in the borealis input manager, so touches never become mouse
clicks either. In `SDLSurface.java` (Android glue) the comment even documents
`adb shell input mouse tap` — but the hold-tap via touchscreen source is the
reliable one.

### Coordinates

- Display space is landscape 3168×1440; tap/swipe coords are in that space.
- The app renders to a **logical 1280×~582 canvas** scaled ×2.475
  (`ORIGINAL_WINDOW_WIDTH = 1280`), but the SDL glue normalizes touch by the
  window pixel size, so **screen-pixel coordinates are what you want**.
- App window is offset x=121 (camera cutout) — treat display coords as
  authoritative (they are what `input` and the screenshot share).

### Runner implication

`Adb.tap()` must issue a hold-tap:
`adb shell input touchscreen swipe X Y X Y 350`
instead of `input tap X Y`. (Implementation: `scripts/switchfin_adb.py`.)

## Screen map (landscape 3168×1440)

- **Sidebar** (left, scrollable, 5 icons): home y≈143, media-folders y≈284,
  search y≈422, remote y≈566, settings y≈711; x≈138–170.
- **Home tab**: vertical rows «Continue Watching», «Next Up», then one row per
  library view (Новинки, Популярні зараз, Фільми, …). Row title ≈y104 first
  row; poster cards ≈y250–850; card pitch ≈464 px, first card center ≈(546,450).
- **Media folders grid** (video-camera tab): the 7 library views as text cells.
  Measured precisely 2026-08-10 by pixel analysis (card = white region on
  #EFEFEF background):

  | Cell | Column x-range | Center | Row y-range | Center |
  |---|---|---|---|---|
  | Новинки (row 1) | 312–1200 | **756** | 100–520 | **310** |
  | Популярні зараз (row 1) | 1248–2128 | **1688** | 100–520 | **310** |
  | Фільми (row 1) | 2176–3064 | **2620** | 100–520 | **310** |
  | Серіали (row 2) | 312–1200 | **756** | 569–1015 | **792** |
  | Аніме (row 2) | 1248–2128 | **1688** | 569–1015 | **792** |
  | Мультфільми (row 2) | 2176–3064 | **2620** | 569–1015 | **792** |
  | Дорами (row 3) | 312–1200 | **756** | ≥1064 (sliver; scrolls) | **1116** |

  Footer bar: Refresh ≈(2358,1345), Back ≈(2685,1345), OK ≈(2950,1345).
- Opening a view (long-press its cell) fires `GET /Users/{id}/Items` — exactly
  what the `open_view_*` steps expect. Opening the first card fires
  `GET /Users/{id}/Items/{id}` + `/Images/Primary`.
- **The grid scrolls**: 7 cells = 3 rows, only rows 1–2 fully on screen at the
  top position. Дорами sits on row 3 — its cell center (756, 1116) is on the
  visible sliver, but the runner must scroll up the grid to bring row 3 fully
  into view before tapping it (see open questions below).

## Verified against the real client (2026-08-10)

- All 7 cells open the **correct** library: tapping a cell fires
  `GET /Users/{id}/Items?parentId=<view-uuid>` and the uuid matches the
  `view_id:` of the corresponding `open_view_*` step in `steps.yaml`.
- A **long-press (350 ms)** on a cell is required (`input touchscreen swipe
  X Y X Y 350`) — a plain `input tap` is dropped (see Input table above).
- The runner's `Adb.tap()` currently emits `input tap` — **it must be changed
  to the hold-tap** before the suite can pass.

## Bugs / findings found on the real device (2026-08-10)

These are real-client behaviors that affect the runner or the backend and
should be fixed or worked around — record new ones here as you find them.

### B1. Cold `/Items` scrape takes ~20 s → app shows "Timeout was reached"

The FIRST `GET /Users/{id}/Items` after a cold backend start takes ~20.8 s
(the backend scrapes all providers to build the view). The Switchfin client
has its own request timeout (~15 s): it pops a "Timeout was reached" dialog
(Refresh / Back / OK) while the backend request is still running. The request
does eventually complete 200 — the dialog is a client-side display, not a
backend failure. A WARM open (second tap on the same view) takes ~5 ms.

**Runner impact:** the runner cold-starts the backend (spec D2), so the first
`open_view_*` step would see the 8 s step timeout expire AND the app stuck on
the timeout dialog (the tap's effect is consumed by the dialog). Needs a
warmup strategy: either (a) issue one throwaway `/Items` request per view
before the suite starts, or (b) the first open step gets a longer timeout and
the runner dismisses the dialog, or (c) pre-warm via a curl loop right after
`wait_for_backend`. The dialog itself must be dismissed (BACK or Refresh tap)
before the next tap lands.

### B2. `input tap` is dropped by the app (polling, not event-driven)

`adb shell input tap X Y` sends DOWN+UP in ~50 ms; the SDL client polls
`SDL_GetNumTouchFingers()` once per frame, so the whole gesture is missed.
Use `input touchscreen swipe X Y X Y 350` (hold ≥ ~300 ms) instead. See the
Input table above — `Adb.tap()` in the runner must be changed.

### B3. `adb exec-out screencap -p` corrupts the PNG over wifi adb

Streaming screencap over wifi truncates the PNG (invalid IDAT). Capture to
the device (`adb shell screencap -p /sdcard/phone.png`) then `adb pull`.

### B4. uiautomator dump is useless on this app

Switchfin is a Qt/SDL (borealis) client — it renders its own UI and exposes
no accessibility tree (6 empty nodes, no text). All element location must go
through screenshots + OCR + pixel analysis.

### B5. Backend must run on port 8003, not the runner default 8000

The app's stored config points at `http://192.168.2.166:8003`. The runner's
`--port` default is 8000, which is already taken on this host — pass
`--port 8003` (or the runner needs a config check).

### B6. The runner has NO navigation between steps — but the real flow needs it

The runner only taps (`_run_view_step` → `_safe_tap`); there is no BACK
primitive and no `keyevent` anywhere in `scripts/`. But the real client flow
between the 24 steps of steps.yaml is: grid → tap cell → library listing →
tap first card → detail screen. The NEXT step (`open_view_*` of another view)
taps a grid cell coordinate WHILE THE DETAIL SCREEN IS STILL OPEN — the tap
lands on the detail page, not the grid. Same problem inside the play steps:
after `first_episode` auto-plays, the screen is the player; the next step
would tap a detail-screen coordinate inside the player.

**The runner must navigate back between steps.** Options: (a) add a `back`
step type (`adb shell input keyevent KEYCODE_BACK`) between per-view steps,
(b) add a `goto: grid` navigation field. This is a runner-code change, not a
steps.yaml tweak.

### B7. Series play: the episode-row tap AUTO-PLAYS (no separate play_button)

On the real client, the series flow is NOT `seasons_tab → first_season →
first_episode → play_button` as steps.yaml assumes. Verified on-device:

1. Opening the series detail fires `GET /Shows/{id}/Seasons` AUTOMATICALLY
   (doSeason in the MediaSeries constructor) — no tap needed. So the
   `seasons_tab` step would TIME OUT waiting for a request that already fired.
2. Tapping the season card fires `GET /Shows/{id}/Episodes` ✓ (the
   `first_season` step matches this).
3. Tapping the FIRST EPISODE ROW immediately fires `POST /Items/{id}/
   PlaybackInfo`, `GET /Videos/{id}/stream`, `GET /Videos/{id}/segment`,
   `POST /Sessions/Playing` — the row tap IS the play action. There is no
   episode-detail screen between the list and playback.

So the corrected series branch is `first_season → first_episode` where the
play expects (PlaybackInfo, stream, Sessions/Playing) move onto the
`first_episode` step. `seasons_tab` either disappears (its /Seasons request
is a side effect of the detail open — covered by the `open_first_card`
step's window) or becomes a no-expect step.

### B8. Fresh vs scrolled coordinates differ (the runner must tap fresh positions)

Calibrated on-device (landscape 3168×1440), all in the DEFAULT (unscrolled)
screen state, verified by the requests they fire:

| Element | Coords | Verified request |
|---|---|---|
| view_newest_x | (756, 310) | `/Items?parentId=ac357…` |
| view_popular_x | (1688, 310) | `/Items?parentId=ddb2b…` |
| view_movie_x | (2620, 310) | `/Items?parentId=94fd7…` |
| view_series_x | (756, 792) | `/Items?parentId=11004…` |
| view_anime_x | (1688, 792) | `/Items?parentId=a67dd…` |
| view_cartoon_x | (2620, 792) | `/Items?parentId=6df35…` |
| view_dorama_x | (756, 1126) | `/Items?parentId=fea6f…` (sliver tap) |
| first_card | (294, 448) | `/Items/{gk}` + poster |
| play_button (movie) | (825, 470) | PlaybackInfo + stream + Sessions |
| first_season | (266, 1200) | `/Shows/{id}/Episodes` |
| first_episode | (995, 135) | PlaybackInfo + stream (auto-play) |

Сезон 1 card is at (266, ~1200) in the FRESH detail (Seasons header at
~y1074); after scrolling it moves (was at (266, 1221) — close but the fresh
value is what the runner needs, it never scrolls).

### B9. Timeout dialog dismisses via BACK, not the Refresh footer button

The "Timeout was reached" dialog (B1) does NOT dismiss via the on-screen
Refresh button tap — use `adb shell input keyevent KEYCODE_BACK` (sometimes
×2). BACK also exits detail → library → grid (it is the app's global back).
The footer Back button (2685, 1345) does navigate one level back on detail
screens (no sidebar there), but keyevent BACK is more reliable.

### B10. First full-suite run (2026-08-10, warmup phase added): 5/7 detail, 2/2 play ❌

Full 37-step run with the warmup phase: 2 handshake ✅, 7 warmup ✅, all 7
opens ✅ (warmup beat B1 — no timeout dialogs), 5/7 `open_first_card_*` ❌,
2/2 plays ❌, 0 PlaybackInfo in the whole run. Snapshot windows prove the
detail taps WORKED (`GET /Users/{u}/Items/{gk} -> 200` + `/Similar` fired for
newest/popular/series/anime/dorama) — the step failed on the SECOND expect:

- **`/Items/{id}/Images/Primary` does NOT fire on the detail screen.** The
  runner expects it after the gk request, but the real client only fetches
  posters during GRID load. Movie/cartoon passed spuriously (grid poster
  lines landed in their step windows). **Fix: drop the poster expect from
  `open_first_card_*` — the gk request alone proves the detail opened.**
- **`play_button` tap at (825, 470) fires nothing.** The coordinate was
  calibrated on «володар стихій» (Новинки detail); the movie library's
  first card (g2:0dd977…) has a different detail layout, so the Play pill
  sits elsewhere — and/or the runner taps before the detail renders its
  buttons (the step taps ~0.1s after the gk line matches). **Fix needs a
  dynamic play-button locate (teal-pill pixel scan or OCR "Play") with
  retry, not a fixed coordinate.**
- 13 runner-user + 7 app-user `/Items` in the capture: the warmup requests
  use the runner's user (013286…) while the app uses its stored session
  (de2bc…) — the backend's per-view scrape cache is shared across users, so
  warmup still works; the capture fixture just carries both users' lines.

### B11. Library screen re-fetches `/Items` in a ~0.6s loop (observed post-restart)

After `am force-stop` + relaunch, opening the Фільми library caused
continuous `GET /Users/{u}/Items -> 200 (1ms)` every ~0.6s for minutes, and
the first-card tap did not register during the loop. Not observed during the
clean run (opens fired once each), so it may be a stuck state after the
"Timeout was reached" dialog (B1) — needs a controlled reproduction: fresh
backend + warmup, then cold-open a view to trigger the app timeout, then
observe whether the library screen enters the loop.

### B12. App shows "Could not connect to server" after backend restart

The app does not silently recover when the backend restarts: it shows a
"Could not connect to server" dialog (persists until `KEYCODE_BACK`), and
after dismissal it re-polls `/System/Info/Public` and recovers. Normal app
behavior, but the runner must expect it after a backend cold-start — a
`BACK`-to-dismiss step before the first open step keeps the suite honest
when the app was already running before the backend came up.

### B13. Cold per-item scrapes blow the step timeout (Seasons 22s, episodes, streams)

Second full run: `play_series` fired PlaybackInfo but timed out, and
`play_dorama` died at first_season — `GET /Shows/{id}/Seasons -> 200
(22074ms)` is a COLD 22s scrape for a series the app never opened before.
The warmup phase only primes VIEW listings (`/Items?parentId=view`), not
per-item content (Seasons/Episodes/PlaybackInfo/stream). Any series the
runner opens for the first time pays the cold price inside the 8s step
window. Options: extend warmup to the play steps' per-item endpoints
(content-churn makes this fragile), raise the step timeout, or have the
backend warm per-item content during the view scrape.

### B14. Content churn breaks the Type probe — the movie view's first card is a SERIES

The movie library's first card changed between runs (DateCreated sort):
`play_movie`'s window shows `GET /Shows/g2:702cb6…/Seasons` — the app
opened a SERIES detail, but the runner's probe returned "Movie" and tapped
play_button (no pill on a series detail) -> locate+retry timeout. The gk
capture (first `/Items/{id}` line in the detail window) can race grid
fetches, so the probed item may differ from the card the tap opened.

### B15. Stream endpoint answers 206, the expect only allowed (200|302) — FIXED

`GET /Videos/{id}/stream -> 206` (range/partial) is a legitimate stream
response; the play expects only matched 200/302, so `play_series` timed out
even though PlaybackInfo + stream + Sessions/Playing ALL fired. Fixed in
steps.yaml: `(200|206|302)`. (The passing Новинки movie streamed 302.)

### B16. open_view_anime re-scraped despite warmup

The anime open's step window shows provider scrapes (hdvbua, eneyida)
mid-flight — the app's anime `/Items` re-scraped ~70s after the warmup,
despite `cache_home_s = 1800`. Unclear why the warm cache wasn't reused
(possibly the app's `includeItemTypes=Series` variant hits a different
cache key, or the view scrape is per-query). Needs a controlled check.

## Run #4 (2026-08-10, after B15) — verdict FAIL: 25 passed, 6 skipped, 6 failed

`Серіали` became the **first fully-passing view** (open + detail + play,
real streaming: PlaybackInfo -> stream 206 -> HLS segments ->
Sessions/Playing 204) — the B15 status-allowlist fix was verified live.
New failures, grouped by cause:

### B17. Stale app grid — the movie first card 404s on detail

`open_first_card_movie` timed out: the app tapped a card whose detail
returned `404 item_unavailable` in 70ms (`GET /Users/{u}/Items/g2:068dde…`),
while `/Similar` for the same id returned an empty 200. The id is NOT in
the current movie grid (verified by re-listing) — the app held a stale grid
from before the backend restart/catalog change. Root fix is runner-side:
**restart the app (force-stop + relaunch) at suite start** so the grid is
never stale, and wait for readiness before the first tap.

### B18. First open step races the app's cold start

`open_view_newest` (the first UI step) timed out with ZERO app requests in
its window — the app was still connecting (it had only just re-authed and
loaded its default view ~11s earlier) when the tap landed. Same fix as B17:
wait for the app to be ready before driving.

### B19. 4×BACK after playback does not return to the grid

After `play_series` streamed, `back_to_grid_after_series` passed (4 BACKs)
but the app landed on Home, not the views grid — `open_view_anime` then
fired nothing (its window is full of Home's background catalog merges:
kinotron/ashdi/bambooua/animeon episode-walks). The nav steps need a
screen-state check (e.g., assert a `/Views` or grid request, or detect
Home and press an extra BACK), not blind fixed-count BACKs.

### B20. Detail/play cold-scrape timeouts persist (B13 recurrence)

`open_first_card_popular` timed out on a 17.5s cold detail scrape
(`GET /Users/{u}/Items/g2:702cb6… -> 200 (17526ms)`); `play_cartoon`
locate+retry found no pill (window shows only animeon scraper warnings —
the detail never finished rendering); `play_dorama` first_season/first_episode
both timed out (window full of catalog merges). Warmup covers grids only —
the per-item detail/Seasons/Episodes paths are still cold. Tracked in
#205 (backend perf) and #210 (runner warm path).

## Run #5+#6 (2026-08-10, after #208/#209-B21) — verdict: converging

### Run #5: the restart exposed B21 — a fresh launch lands on HOME

The #208 relaunch worked (fresh grid, no stale ids) but a fresh Switchfin
launch lands on HOME (Continue Watching / Next Up / per-view
`/Items/Latest` rails), while the open steps' `view_*_x` taps expect the
Views grid. All 7 opens fired zero requests. Fixed: the sidebar "media
folders" icon (calibrated (141, 285), fires `GET /Users/{u}/Views`)
opens the grid; `_run_restart` taps it after reconnect and waits for
/Views (commit e175229). Sidebar icon centers measured from pixels:
Home (108, 148), folders (141, 285), search (147, 424), cloud (157,
565), gear (153, 708).

### Run #6: 2/7 views fully pass; remaining failures are the known deep ones

- **Новинки and Серіали fully pass** (open + detail + play, real
  streaming) — the B15 + #208 + B21 fixes are verified end-to-end.
- **open_view_anime ❌ (B19)**: the only open failure. After `play_series`
  streamed and `back_to_grid_after_series` (4×BACK), the app was NOT on
  the Views grid — the anime tap (1688, 792) opened a DETAIL (item +
  /Similar for g2:749eead9…) instead of the grid. Nav after playback
  still needs a screen-state check (#209).
- **open_first_card_popular ❌ (B13)**: the popular first card is the
  same series g2:702cb6bcfd5dcce1 whose detail cold-scrapes 16.8s
  (window: Similar + SpecialFeatures fired, the item itself took
  16797ms in play_movie's window) — blows the 8s step window (#210).
- **play_movie ❌ (B14 + B13)**: the movie view's first card is a SERIES
  (`/Shows/g2:702cb6…/Seasons -> 200 (15471ms)`) but the Type probe
  returned Movie → the play branch tapped play_button and locate+retry
  timed out (#206).
- **play_cartoon / play_dorama ❌ (B13)**: first_season/first_episode
  taps timed out on cold per-item scrapes (#210).

The real-client capture fixture was regenerated by run #6 and is now a
better contract: 0 `startIndex=18` lines (the B11 pagination loop is gone
from the recording) and 11 real play lines (PlaybackInfo / stream /
Sessions/Playing).

## Run #7 (2026-08-10, after #210 warm) — 3/7 views fully pass

Популярні зараз joined the full-pass list (open + detail + play — the
#210 first-card warm fixed its 16.8s cold detail). Новинки and Серіали
pass again. Remaining failures, all mapped:

### B22. Play steps share the 8s step deadline — the app reports
Sessions/Playing ~5s after the tap (FIXED)

play_newest fired PlaybackInfo (294ms) + stream 200 + HLS segments but
its Sessions/Playing landed at +5s, just past the 8s deadline — the
locate+retry loop's screenshot attempts pushed the effective tap late.
Fixed: play steps get their own `PLAY_TIMEOUT_S = 25s` window
(`max(timeout_s, play_timeout_s)`); the runner unit test feeds a delayed
Sessions/Playing to pin the tolerance.

### B19 recurrence (open after playback)

open_view_movie and open_view_anime timed out — in both windows the tap
opened a DETAIL (a series item + /Similar), not the Views grid: after
play_popular and play_series streamed, the 4×BACK nav left the app on a
detail screen. Same root cause as run #6 — tracked in #209. The open
steps that FOLLOW a non-played view still work.

### Play paths with zero requests (cartoon, dorama)

play_cartoon (movie branch: locate+retry found no pill) and play_dorama
(series branch: first_season/first_episode) fired ZERO backend requests.
The empty-window backend-*.txt files are intentionally not rewritten
(#149), so the stale run-#6 files linger — the logcat-*.txt files carry
run #7's real content. Needs an on-device look at the cartoon/dorama
detail screens (layout/probe suspicion, #206).

## Run #8 (2026-08-10, after B22) — 2/7 fully pass; B23 found

Новинки + Серіали pass again. The #210 first-card warm now covers most
views (fast detail/Seasons/Episodes/PlaybackInfo chains for stable first
cards), but two failure classes remain:

### B23 (NEW, backend): heavy series intermittently 404 on detail

The warm's detail for the ~137-episode series g2:702cb6bcfd5dcce1
returned `404 item_unavailable` after 12.9s at 22:02:01; the app's
request for the SAME id returned `200 (25785ms)` at 22:02:53 and
`/Shows/{id}/Seasons -> 200 (27934ms)`. A valid item transiently 404s
after a long scrape, then succeeds. Besides showing the app an error, the
404 broke the #210 warm chain (no Type in the 404 body -> the series play
path was skipped), leaving popular/movie play cold. Ticket #211.

### Churn still beats the warm for the movie first card

The movie view's first card at warm time (22:02:01) was g2:0dd97776…; at
play time (22:02:53) the app tapped g2:702cb6… (DateCreated sort shifted)
— the same B14 race, now between warm and play (#206).

## Run #9 (2026-08-10, after #211 retry) — 3/7 fully pass

Новинки + Популярні + Серіали pass fully (Популярні's play passed for
the first time — the #211 retry kept the heavy-series warm chain intact
this run; no 702cb6-related failures). The remaining failures are exactly
two classes, every run:

1. **B19 (#209) — the open step after a played view fails.** Run #9:
   play_popular streamed -> open_view_movie tapped a SERIES DETAIL
   (g2:b9ae09f3, the same item as run #7) instead of the Views grid;
   play_series streamed -> open_view_anime failed the same way. The 4×BACK
   nav after playback does not return to the Views grid. This is now THE
   suite blocker: it costs 2/7 views' open+detail+play every run.
2. **Play taps with zero requests (#206, B14 probe race).** play_cartoon
   (movie branch: locate+retry found no pill) and play_dorama (series
   branch: first_season/first_episode) fired NOTHING — the Type probe's gk
   can race grid fetches, so the branch may not match the card the tap
   opened. Backend-*.txt windows stay stale (empty windows aren't
   rewritten, #149); logcat-*.txt are empty too.

## Run #10 (2026-08-10, after #209 visual nav) — nav fixed, probe phase

Run #10's FIRST nav attempt used the folders tap + `/Views` HTTP wait — it
failed: after the app has launched once, the Views list is cached
CLIENT-SIDE, so tapping the folders icon opens the grid with NO request.
The HTTP signal was wrong.

A manual probe session then mapped the real screen stack (with the 350ms
hold tap, `input touchscreen swipe X Y X Y 350`):

| Screen | Signature |
|---|---|
| Player | ~0.96 dark (video) |
| Detail | light, poster left, teal play pill, no X |
| Library grid | light, top tabs (Home/Genres/Suggestions) + **X close at
  top-right ≈(3100, 55)**, cards |
| **Views grid** | light, sidebar rail (5 icons, film teal-active), 2×3
  view cards, **no X** |

Empirical BACK depth from the player: **4** — player → detail → library →
(the player's exit transition SWALLOWS a BACK) → Views grid. A fixed
count therefore drifts; the fix (#209, `363c48c`) is **visual
verification**: the nav step presses BACK until `find_views_grid()` (a
pixel classifier: no X cluster + light frame + sidebar icon rail at the
calibrated positions, luminance-normalized against the phone's idle
4%-brightness dimming) matches a screenshot, up to `NAV_MAX_BACKS` (6),
then falls back to the sidebar folders tap, then fails honestly. The grid
opens client-side, so arrival CANNOT be verified over HTTP.

Device notes from the probe:

- The phone dims to ~4% after idle (`mScreenBrightnessOverrideFromWindow-
  Manager=0.04`) and the player sets its own dimming — screenshots during
  long manual probes look black; normalize before thresholding, or
  `settings put system screen_brightness 255` / `KEYCODE_BRIGHTNESS_UP`.
- `input tap` (DOWN+UP in ~50ms) is DROPPED by the client; the 350ms hold
  (`input touchscreen swipe X Y X Y 350`, B2) is the only working tap —
  every manual tap in this probe used it.
- `.scratch-phone-preview/shot.py` re-embeds the latest screenshot into
  the preview HTML (the preview server serves only the registered HTML, so
  relative image paths 404); `classify_test.py` re-validates the nav
  classifier against labeled captures. Both are gitignored session tooling.

## Run #11 (2026-08-10, after #209 visual nav) — B19 gone, 4/7 plays

**The suite blocker is dead.** All 7 opens + 7 details + 7 nav steps pass;
4/7 plays pass (Новинки, Популярні, Серіали, **Аніме — first time ever**).
Every `back_to_grid_after_*` step succeeded via the visual classifier
(note: "reached Views grid after 3/4/5 BACK(s)" — the swallowed-BACK
depth varies per run, confirming that a fixed count could never work).

Remaining failures are ALL play-step class (B13/B14, #205/#206/#210):

- **play_movie/cartoon** — `play_button: timeout (locate+retry)`: the app
  opened the detail (movie g2:019beb1c fired Items+Similar, no Seasons),
  but no PlaybackInfo ever fired — the teal pill locate + calibrated tap
  missed. Screenshot dimming after the previous view's player exit is one
  suspect (find_play_pill does not luminance-normalize; find_views_grid
  does).
- **play_dorama** — `first_season/first_episode: timeout`: the series
  branch taps landed but the stream never started (probe race, B14).

The regenerated capture fixture now shows clean one-page pagination (each
view: startIndex 0→18 once — the B11 fix in the contract) and 24 play
lines (4 plays × PlaybackInfo+stream).

## Run #12 (2026-08-10, after #206 gk capture) — 5/7 plays

- **play_movie FIXED** (the gk-capture + pill-normalization fix, e135635).
- **play_cartoon now takes the SERIES branch** — the gk capture correctly
  identifies the first card as a series now (it had been mis-branching to
  the movie pill), but first_season/first_episode still fired nothing.
- Failures moved: play_popular regressed (pill locate, zero requests),
  open_view_dorama flaked (tap at 756,1126 fired no /Items — the 7th
  card is only partially visible at the grid's bottom edge). The two
  bottom-area taps (view_dorama_x 756,1126 and first_episode 995,135)
  are the flakiest — both failed this run.

The remaining failures are intermittent content-churn / cold-scrape races
in the play taps (the #205/#210 class) plus occasional bottom-edge tap
misses. The runner itself is stable: nav, open, and detail pass 100%.

## Run #13 (2026-08-10, after #205 tap retry) — 5/7 plays

- **play_cartoon FIXED** (the #205 series-tap retry). The failures now
  rotate: play_movie (pill locate, zero PlaybackInfo — the detail opened
  g2:0dd97776 at 23:50:59 but nothing fired in 25s) and play_dorama
  (retried season taps still silent). The movie-detail screen WAS open and
  warm, so the pill locate is either missing a dimmed/wrong-position pill
  or tapping a false teal positive in the poster art.
- The runner now saves `screen-<step>.png` for failed play steps (#205
  follow-up) — the next run's failures are visible instead of empty
  windows.

### B24 (NEW, backend): ufdub dorama titles have empty Seasons — unplayable

A dorama detail (g2:431a703472c60b12, "Камен Райдер Ґавв" / Kamen Rider
Gavv, ufdub provider) opened as `Type=Series` but `/Shows/{gk}/Seasons`
returned `Items: []` — so the app renders NO season rail, the runner's
first_season/first_episode taps hit nothing, and play_dorama timed out
with zero PlaybackInfo requests (runs #13/#14). Reproduced live against
8003:

- `GET /Users/…/Items/g2:431a703472c60b12` -> 200, `Type: Series`
- `GET /Shows/g2:431a703472c60b12/Seasons` -> 200, `Items: []`

Root cause: ufdub's `content()` builds `seasons` only for
`media_type in ("series", "anime")`, but `_type_from_url` maps
`/dorama/` to `"dorama"`. The catalog classifies doramas as Series
(form=series), so the app always asks for Seasons and gets an empty
rail. bambooua/doramyworld/uaflix all build seasons for dorama; ufdub
was the lone gap — and since `resolve_group_content` uses the group's
FIRST provider, the failure was provider-order-dependent (flaky across
catalog builds).

Fix: ufdub `content()` now builds the single season for
`media_type in ("series", "anime", "dorama")` — doramas are serialized
content with the same player-page `var a` episode list. Verified live:
`/Seasons` now returns Сезон 1 (`g2:…:S1`), `/Episodes` returns all 50
episodes (`ufdub:dorama-408-…:s1e1…`). Regression test
`test_ufdub_content_dorama_gets_single_season`.

## Run #14 (2026-08-10, after #205 series-tap retry + failure screenshots)

- Новинки + Серіали + Аніме + Мультфільми pass fully (4/7 views),
  including play. **play_newest passed for the first time.**
- **open_first_card_popular / open_first_card_movie ❌** — first-card taps
  missed (same tap-miss class as play; the bottom-row dorama open flaked
  in #12). Their plays skipped.
- **play_dorama ❌ → B24.** The failure screenshot (`screen-play_dorama.png`)
  showed a fully-rendered detail with NO season rail; the backend log
  showed the detail + `/Seasons -> 200 (0ms)` but zero PlaybackInfo. Live
  API repro proved the Seasons response was `Items: []` — a backend bug,
  not a tap miss. Fixed + verified live (Сезон 1 + 50 episodes).
- The new failure screenshots paid for themselves: `screen-play_movie` /
  `screen-play_newest` (identical, blank light frames — saved when the
  pill scan found nothing on an unloaded grid) vs `screen-play_dorama`
  (real detail, teal pill, no rail).

## Run #15 (2026-08-11, after B24 ufdub dorama fix) — 6/7 views pass

- **play_dorama FIXED (B24)** — the dorama detail now renders its season
  rail (Seasons returns Сезон 1 + 50 episodes) and the runner's
  season/episode taps fire PlaybackInfo. Best run so far: Новинки +
  Популярні + Серіали + Аніме + Мультфільми + Дорами fully pass
  (6/7); only play_movie fails.
- **play_movie ❌ — NEW diagnosis via the failure screenshot.**
  `screen-play_movie.png` (the screenshot infra added after #205) shows
  the detail FULLY rendered with the teal pill visible — yet the pill
  scan returned None and play_button timed out. Debugging
  `find_play_pill` on the actual frame:
  - The movie poster ("Перша поїздка") has a WIDE teal banner at the
    bottom (x=71..597, 526px) that beat the 259px pill on width.
  - The scan validated only the widest run, and the banner's vertical
    band was measured at the poster's own (tall) teal column — the
    "lowest band" heuristic assumed hero art above the pill, which the
    poster's own content below breaks.
  - Result: None → runner fell back to the calibrated tap → missed →
    timeout, despite a perfectly rendered pill.
  - Fix: candidates tried in width order; the band must CONTAIN the
    candidate's row (not the lowest band); `tealish` now requires
    green-dominance (g-r >= 60 — poster art is desaturated blue, the
    pill is always green-dominant); aspect-ratio filter rejects wide
    flat strips (>6:1). Verified: movie pill now (824, 297), dorama
    unchanged (824, 336). Two regression tests.
- Run #15 also refreshed the capture fixture (contract suite still
  green) and confirmed the blank-frame screenshots (`screen-play_newest`
  from #14) are genuinely blank (grid unloaded, not a rendering bug).

## Run #16 (2026-08-11, after the first pill-scan fix) — 6/7 again

- play_movie STILL fails: the pill scan returned the poster again. The
  movie was different ("Я матюкаюсь", bright-blue poster) and exposed
  the NEXT hole in `find_play_pill`: the per-row scan kept only the
  row's WIDEST teal run — the poster's blue column was wider on the
  pill's rows, so the pill (split by its white "Play" text into two
  ~250px segments) was never even collected. The returned point
  (334, 917) was a lower poster region passing every check.
- Fix: collect EVERY run ≥ min_w per row (multiple per row), keep the
  green-dominance color filter, band-containing-row, aspect-ratio and
  position (top 55%) filters. Verified against three real frames:
  dorama (824, 336) unchanged, movie #1 (824, 297) and movie #2
  (824, 297) now correct.
- Note: the docs/ `screen-play_movie.png` reverted to the committed
  blank frame at teardown (twice — runs #15 and #16) for an unknown
  reason; the real failure frames are preserved in
  `.scratch-phone-preview/fail-movie1{5,6}.png`.

## Run #17 (2026-08-11, after the collect-all-runs pill fix) — **FULL PASS** ✅

**All 7 views pass completely** — open + detail + play + nav for Новинки,
Популярні, Фільми, Серіали, Аніме, Мультфільми and Дорами, with real
streaming on every play. First fully-green run of the campaign (runs
#4-#16 never exceeded 6/7). play_movie fixed by the collect-all-runs
pill scan; the B24 dorama fix (60ac255) held again.

## Run #18 (2026-08-11, stability check) — **FULL PASS AGAIN** ✅

Second consecutive fully-green run (all 7 views: open + detail + play
with real streaming + nav). The full pass is stable, not luck — runs
#17 and #18 both complete. The suite can now be trusted as a regression
net: any backend regression that breaks a view's open/detail/play will
show up as a red run.

## Open questions (for the next session)

- DONE (2026-08-10): `Adb.back()` + `phase: nav` steps wired into the runner;
  `seasons_tab` removed from the series flow (B7); warmup phase (B1); detail
  poster expect dropped (B10); dynamic play-pill locate + retry (B10/#202);
  /Items startIndex/limit pagination honored (B11/#203); stream expect now
  allows 206 (B15); app restart + readiness (B17/B18/#208); B21 folders-tap
  restart nav; **B19/#209 nav now verifies the Views grid VISUALLY (pixel
  classifier, no HTTP — the grid opens client-side after the first launch)**;
  #201/#202 closed. See `git log` on this branch.
- **B13/B20**: cold per-item scrapes still blow the 8s step timeout — extend
  warmup to the first-card detail path or the step timeout / backend warm path.
- **B14**: the Type probe races content churn — probe the item the app
  actually opened, or make the branch decision tolerant (try the pill scan;
  if absent, fall through to the series branch).
- **B16**: why did the anime view open re-scrape after a 70s-old warmup.

## Running the suite (2026-08-10, codified)

```bash
cd ps4-uk-stream
backend/.venv/bin/python scripts/switchfin_test.py --skip-calibrate --port 8003
```

The runner cold-starts its own backend on the port and drives the phone
over WiFi adb (`192.168.2.143:38631`). Lessons:

- **Never launch it with a bare `&` from a 30s-timeout terminal call** — the
  command runner reaps the process group. Use `setsid … &` (survives) or a
  long timeout, and verify the backend.log mtime advances.
- **A leftover uvicorn on the port silently hijacks the run**: the runner's
  own uvicorn fails to bind, `wait_for_backend` connects to the leftover
  (possibly pre-fix code, wrong capture env), and the run verifies against
  stale logs. Before each run: `ss -tlnp | grep <port>` must be empty and
  `pgrep -f uvicorn` must show only the runner's own child (lstart AFTER
  launch).
- **Python buffers stdout when redirected** — pass `-u` to see step
  progress in the run log.
