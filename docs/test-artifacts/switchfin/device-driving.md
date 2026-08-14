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
| `input keyevent KEYCODE_MENU` (82) | ✅ | **Opens the card context menu** (Played / Favorite / Download) on the focused card — measured 2026-08-14. `BUTTON_Y`/`BUTTON_X`/`ENTER` do NOT open it |
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

- **Sidebar** (left, 5 icons, NOT scrollable — measured 2026-08-14): home
  y≈143, media-folders y≈284, search y≈422, **Downloads y≈567** (the cloud
  icon — NOT Remote; earlier run mislabeled it), settings y≈711; x≈138–170.
  **There is no Remote tab and no Live TV entry in the reachable UI of
  Switchfin 0.9.3** — the `RemoteTab`/`LiveTV` classes exist in
  `libSwitchfin.so` but are not wired into any reachable screen, so
  `GET /Sessions` / `GET /LiveTv/Channels` are never called by the client.
- **Home tab**: vertical rows «Continue Watching», «Next Up», then one row per
  library view (Новинки, Популярні зараз, Фільми, …). Row title ≈y104 first
  row; poster cards ≈y250–850; card pitch ≈464 px. **Measured 2026-08-14:**
  the Continue-Watching card centers are ≈(930, 450) for card 1 (Аанг),
  ≈(1786, 450) for card 2, ≈(2640, 450) for card 3 — a tap at (546–680, 400)
  misses card 1 (hits the left gutter) or lands on another card after a
  relaunch, so always verify which item the detail fetch returns in the log.
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

**Re-confirmed 2026-08-14 (still open):** an app **relaunch** (force-stop +
start) also triggers it — the home `GET /Users/{id}/Views` took 20511 ms and
poped the same dialog; the Retry button at ≈(1130, 885) (dialog buttons ≈y885,
Retry left / Cancel right) reloads instantly from the now-warm snapshot.

### B4. Download button 404s — `GET /Items/{id}/Download` route missing

(2026-08-14) The detail screen's Download button calls
`GET /Items/{id}/Download?api_key=…` → **404** — the backend only has
`/Videos/{id}/stream`. The app records the failure in
`/sdcard/Android/data/fun.dragonfly.switchfin/files/downloads/index.json`
as `status: Failed / http status 404`. Follow-up: **ticket #296**.

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
| view_recent_movie_x | (756, 310) | `/Items?parentId=ef3d4…` (ESTIMATE — re-verify, #263 re-layout) |
| view_recent_series_x | (1688, 310) | `/Items?parentId=774bd…` (ESTIMATE — re-verify, #263 re-layout) |
| view_popular_x | (2620, 310) | `/Items?parentId=ddb2b…` (ESTIMATE — moved to the third grid slot, #263) |
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

### B25 (NEW, backend ops): long-running process wedges — ALL upstreams
unreachable while cached 200s still serve

The user's "Президент Кертіс" (President Curtis) did not play: the app's
`GET /Items/g2:1122d60d5c637572` 404'd `item_unavailable` after ~17s
(two 8s-slot attempts). Live probe showed the SAME process (uptime 13.5h)
reporting `upstream_unreachable` for **all 18 providers simultaneously**
(empty error text, exactly the 8s upstream timeout), while a freshly
started process reached the same sites in <2s and resolved the item
(g2:814abd4799777d9c, 3 episodes, PlaybackInfo 200, manifest 200).

Root cause: not a code bug — the long-running uvicorn's outbound
connectivity silently died (network change / VPN-state change under it),
and nothing restarts it. The health tracker recorded the failures but no
watchdog acted. The app's stale key also re-resolved fine after restart
(it maps to simpsonsuatv's "Колишня" S01E03 — the item's Overview shows
"Президент Кертіс 1 сезон 3 серія").

Fix (operational): restart the backend. Prevention: a watchdog / health
endpoint that restarts the process when ALL providers go down
simultaneously. Ticket #215.

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

## Untested-screen probe (2026-08-11) — Home/Search/Suggestions OK, Genres + Resume empty

Walked the app screens the suite does not cover (manual, backend-log
witnessed):

- **Home** ✅ — Новинки / Популярні зараз rows render with posters.
- **Search** ✅ — on-screen keyboard (6 rows × 6 keys, step 132px, first
  key center x=387; rows at y≈500/633/753/873/1005/1122); incremental
  `searchTerm` requests (AVATA → AVATAR); results populate the Suggest
  column; backend search works (Latin matches Ukrainian titles, e.g.
  "avatar" → 80 hits).
- **Suggestions tab** ✅ — fires Resume + Latest + NextUp; the Latest
  shelf populates the grid.
- **Genres tab** ✅ (after #213) — `GET /Genres` returns the aggregated
  shelf (ufdub card genres: Історія, Драма, Кримінал, Психологія, Фільми…).
  Tapping a genre fires `/Items?genreIds=<name>&includeItemTypes=Movie`
  and the filtered grid renders (verified live 2026-08-11: Кримінал → 18+
  crime movies, paginated to startIndex=18). Note: «Фільми» appears as a
  genre — a provider card-metadata quirk (form label in the Жанр block),
  faithful aggregation, not a facade bug.
- **Continue watching / Next up** ❌ (before #214) — `/Items/Resume` and
  `/Shows/NextUp` always empty because `Sessions/Playing/Stopped` etc.
  answer 204 and store nothing (D8). Ticket **#214** — FIXED below.

## #214 implemented (2026-08-11) — playback progress → Resume / NextUp

`Sessions/Playing/Stopped` now stores `ItemId → PositionTicks` in a
process-wide store (`catalog_state.playback_positions`); `/Items/Resume`
resolves each id to its DTO (g2: movies directly, episode wire ids through
the group map) and stamps `PlaybackPositionTicks`; `/Shows/NextUp` returns
the next sibling of the most-progressed episode per series. `clear_playback()`
keeps tests isolated; a malformed report body still 204s (advisory, D8).

**Live verification (device + API):** POSTed a real episode wire id
(`ufdub:dorama-…:s1e1`, PositionTicks=900000000) → `Resume` returns the
episode with the position, `NextUp` returns episode 2 of the series. After an
app restart, the phone's Home renders **Continue Watching (1)** and **Next Up
(1)** rails with the Kamen Rider cards — the exact flow that was empty in the
probe. Tests: 931 pass, ruff/mypy clean (only pre-existing findings).

**#234 (2026-08-14, FIXED, `6015681`): the reverse group lookup only
worked for providers whose episode wire id prefix EXACTLY equals the
card id — so Continue watching stayed empty for the two biggest
providers.** uakino emits `{provider}:{news_id}:eN` while its card id
is the full `{provider}:{section}:{news_id}-{slug}` composite
(`uakino:6268:e1` vs `uakino:anime-series:6268-narutto-1-sezon`), and
animeon appends a base64 source blob AFTER the `:eN` tail so the
episode-tail regex `:(?:s\d+)?e\d+$` never matched. Fixed by tolerating
the blob in the tail regex (`(?=:|$)`) and falling back to a
same-provider numeric item-id segment match in `group_key_for_external`.
Live wire: played `uakino:6268:e1` → Resume returns it with
PlaybackPositionTicks=36000000000, NextUp returns `uakino:6268:e2`
(both empty before). Tests: 1016 pass (+2 new).

## Detail-screen unfilled-data probe (2026-08-11, runs 12 + manual) — ufdub description + People rail FIXED (#225)

Continued the unfilled-data pass on the live device after run12 (third
consecutive FULL PASS):

- **Description** ❌→✅ — ufdub detail pages rendered a blank description
  area even though `div.full-text` had one: the block opens with an EMPTY
  spacer `<p>` and the real description in the second paragraph, and the
  parser's `select_one("div.full-text p")` grabbed the empty one. Fix:
  first non-empty paragraph (commit `6f32b60`). Live-verified on Kamen
  Rider Gavv — the phone's detail now shows the full synopsis (wire
  Overview 398 chars). Other providers select containers, not children,
  so the bug was ufdub-unique.
- **People rail** ❌→✅ — ufdub never parsed its dubbing-team credits
  (`div.voices` blocks: «Куратор проєкту», «Перекладач», «Редактор»,
  «Актори озвучення», «Робота зі звуком»), so the rail rendered its
  header with zero tiles on every ufdub detail. Fix (commit `8d9bf5d`):
  parse each block into `Person` — id from the person's own
  `/xfsearch/<kind>/<slug>/` page slug, role `Actor` for «Актори
  озвучення» (exact-label match — a substring check would misclassify
  «Редактор»), block label otherwise; «Постер» skipped (poster
  designers are not on-screen crew). Live-verified: the rail now renders
  tiles on the phone (Манюха, Maxx Light, Twilight, Віхенька, Кіт,
  Anomaliya, InSnake…); wire returns 11 people for «Леді Баг та Супер
  Кіт: Париж» and 1 (Перекладач Doctor Os') for Gavv whose page only
  has Постер+Перекладач.
- **Quirk noted** (not a ticket): «Перекладач» names can be telegram
  links (e.g. `https://t.me/lbcnua`) — the site's own link text. If the
  rail ever needs cleaning, filter URL-shaped names at the DTO layer.

## Open questions (for the next session)

- DONE (2026-08-10): `Adb.back()` + `phase: nav` steps wired into the runner;
  `seasons_tab` removed from the series flow (B7); warmup phase (B1); detail
  poster expect dropped (B10); dynamic play-pill locate + retry (B10/#202);
  /Items startIndex/limit pagination honored (B11/#203); stream expect now
  allows 206 (B15); app restart + readiness (B17/B18/#208); B21 folders-tap
  restart nav; **B19/#209 nav now verifies the Views grid VISUALLY (pixel
  classifier, no HTTP — the grid opens client-side after the first launch)**;
  #201/#202 closed. See `git log` on this branch.
- **B25/#215 (NEW)**: a long-running backend can lose ALL outbound
  connectivity while still serving cached 200s — every upstream times out
  at exactly the 8s timeout, detail/play 404 after ~17s. Restart fixes it.
  Prevention: watchdog / health check that restarts on all-providers-down.
- **B1/B13/B20/B16 — RESOLVED (2026-08-11, #204/#210, a6f5d33)**: the backend
  now warms itself at startup — the home snapshot, then each view's first-card
  detail chain via `resolve_group_content` (the one primitive detail/seasons/
  episodes/playback read through). Live-verified on 8003: first `/UserViews`
  3ms, view `/Items` 3ms, first-card detail 2-43ms (all 17-21s cold before);
  the anime view's `/Items?includeItemTypes=Series` (B16 path) serves 20
  items in 3ms — the "re-scrape" was the app auto-firing the first-card
  DETAIL (now warmed), not the grid. State: `/api/health.catalog_warm`
  (`status/home_warmed/content_warmed/failed`); disabled in tests via
  `CS_UK_CATALOG_WARM=0` (conftest), default on.
- **NEW (2026-08-11)**: row-form vs content-form mismatch — a card listed as
  `Type: Series` in a view resolved to `Type: Movie` on its detail (form
  verdict differs between the home row's first-seen source and the detail
  source). The app renders the grid as a series then opens a movie detail.
- **B14 — ROOT CAUSE FOUND + FIXED (2026-08-11, #217)**: the play Type
  probe's `gk` capture regex matched ANY `/Items/<id>` line — and the app
  polls `/Items/Resume` every ~0.5s even with a detail open, so "last match
  wins" (#206) overwrote `gk` with "Resume"; the probe then interrogated
  /Items/Resume (a QueryResult without a top-level Type) and every play
  step failed except the one that won the timing race. Fixed by anchoring
  the capture to card ids (`(?P<gk>g2:[^ /]+)` in steps.yaml) + a probe
  guard + the captured gk in the failure note. **Verified on device:
  run4 = 38/38 PASS, all 7 plays green.**
- **play_newest false-fail (2026-08-11, run5 = 36/37)**: the video was
  ACTUALLY playing (failure screenshot shows the teal progress bar) but the
  step timed out — a cold first stream (~7.6s upstream fetch) stretched the
  PlaybackInfo→stream→Sessions/Playing chain to 21s, racing the 25s
  deadline; meanwhile `_tap_play_with_retry` re-tapped the play pill every
  ~2s, which on a RUNNING player toggles pause. Fixed: once ANY expect line
  appears the tap has landed — stop tapping and wait for the rest of the
  chain (`window_s=remaining`); `PLAY_TIMEOUT_S` 25→45s. Regression tests:
  no re-tap after landing (4 taps before, 2 after) + slow chain passes.
  Also fixed a latent NameError in the probe guard (undefined `log` →
  stderr print): the contaminated-gk path would have crashed, not failed
  loudly. `165d4e4` (amended).
- **UNFILLED DATA PASS (2026-08-11, #218-#223)**: drove the app to 6
  different videos (Таємниця бункера, Легенда про Аанга, Історія палацу
  Куньнін, Kamen Rider, Перша поїздка, Реінкарнація безробітного) and
  catalogued every empty surface:
  - `/Items/{id}/Similar` — always empty (#218) — **FIXED** (`b9f915f`):
    same-genre cards from the cached snapshot; live: «Я матюкаюсь» → 2
    similar (Щелепи, Енн).
  - detail `Genres: []` even where the card parser (#213) harvested genres
    (#219) — **FIXED** (`b9f915f`): `_content_dto` falls back to the
    snapshot card's genres; live: [Фільми, Історія, Психологія].
  - `ProductionYear: None` on 5/6 details; 120/130 home cards carry no
    year — ufdub parses none (#220) — **FIXED** (`d861adf`): ufdub
    `content()` parses the `Рік:` block; detail falls back to the card
    year. Live: Я матюкаюсь→2025, Небесні Створіння→1994, Kamen
    Rider→2024.
  - `People` rail never renders — detail DTO has no cast (#221) —
    **FIXED** (`b5c4e11`): kinotron/uaserialspro actor lists + klontv
    JSON-LD → `People` + `/Persons/{id}`. Live: Ґранчестер → 23 people.
  - rating badge shows `0` — no `CommunityRating` anywhere (#222) —
    **FIXED** (`2d90b63`): klontv JSON-LD `aggregateRating` →
    `CommunityRating`; absent stays omitted. Live: Ґранчестер → 7.9.
  - episodes carry no Overview/RunTimeTicks/PremiereDate even though the
    app requests `fields=...Overview` (#223) — **FIXED** (`2d90b63`):
    animeon now calls the documented-but-unwired `episodes-info`
    endpoint (best-effort) → real episode titles + PremiereDate. Live:
    «Ми з тобою — повні протилежності» епізоди 1-2 → Святвечір
    (2026-07-05), Дилема зимової ночі (2026-07-12).

## Device-state battles (2026-08-11, runs 9-11) — the phone must be UNWEDGED before a run

Runs 9/9b/9c failed NOT because of the code but because the phone's
SystemUI was wedged:

- **Stuck NotificationShade**: the shade window held focus and covered
  the app; `cmd statusbar collapse`, `KEYCODE_BACK`, swipe-up, and
  `CLOSE_SYSTEM_DIALOGS` all failed to dismiss it. The app relaunched
  BEHIND the shade and never made a request → `restart_app` timed out.
- **Pocket mode**: the proximity sensor thought the phone was covered
  ("Pocket mode is on… long press power to force quit") — only a
  `input keyevent --longpress KEYCODE_POWER` dismisses it, and it
  re-engages.
- The screen kept dozing despite a 30-min `screen_off_timeout`.

Remedy that worked (in order): `adb reboot` → wait for boot →
`KEYCODE_SLEEP` + `KEYCODE_WAKEUP` cycle → `wm dismiss-keyguard` +
`cmd statusbar collapse` → the app's server screen appeared clean.
Fixes baked into the runner: per-step `Adb.wake()` (wake + keyguard),
`Adb.keep_screen_on()` (pin `screen_off_timeout` to max for the run,
restore after — `066046b`), and `find_views_grid` now tolerates
non-landscape frames instead of IndexErroring (`39e944e`).

If a run fails with zero phone requests, check the focus FIRST:
`adb shell dumpsys window | grep mCurrentFocus` must NOT be
`NotificationShade`, and the screen must be awake.

## Popular-view flake (2026-08-11, run7+run8) — ROOT CAUSE + FIXED (#224)

Two consecutive runs failed on the SAME popular first card — «Реінкарнація
безробітного» (`animeon:7333`) — in two different failure modes:

- **run7**: `play_popular` — the first-ever `/Shows/Seasons` resolution
  took **37,165 ms** (cold full episode walk, 59 upstream fetches across
  two retry rounds under animeon throttling) → the step deadline expired
  and the app rendered an **empty `| Seasons` rail** (placeholder tiles).
- **run8**: `open_first_card_popular` — animeon answered **502** for both
  retry attempts → the detail handler 404'd → the step's expected `200`
  never came. The app fired Seasons+detail for the same card in parallel,
  re-resolving the key 4× in ~2s (4 upstream hits per failed open).

Root cause chain (evidence in `backend-open_first_card_popular.txt`): the
card was NOT in the warm's `content_cache` because at warm time animeon was
`unreachable` — and the warm masked it: `catalog warm done: content_warmed=5
failed=0` while the popular first card stayed cold (a provider-error `None`
is indistinguishable from a legit unavailable verdict).

Fix (`b8f94ed`, #224):

- `resolve_group_content` is now **single-flight** per cache key — the app's
detail+seasons+playback burst shares ONE upstream resolution and its verdict
(run8's 4-hit storm → 1).
- `item_detail` **degrades to a card-data DTO** when the item IS a known
home card but its live resolution failed transiently (title/type/year/
genres/poster render from the card, mirroring the seasons rail's tolerant
empty answer). Deliberate 404s — cold cache, unknown ids, gated/blocked,
season suffixes — are preserved via the `is_hard_unavailable` guard.
- the warm now reports `cold_keys` (which first cards stayed cold) via
`/api/health.catalog_warm`, and the runner's `wait_for_backend` **gates the
first step on the warm finishing** (`WARM_READY_TIMEOUT_S=300`, disabled-
warm reports `done` so it never blocks).

### Run #10 (2026-08-11, after #224 + device-unwedge) — **FULL PASS ✅**

All 7 views green, including **Популярні зараз: play ✅** — the exact
step that failed in run7/run8. The backend log shows the #224 package
working under the exact run8 scenario: animeon was `unreachable` at
warm time again (`content_warmed=4 failed=0` — now with the cold_keys
report showing the popular card stayed cold), the runner's warmup
retried and landed the detail in 31s, and the actual play step's
Seasons answered in **1ms from the warm cache**. First resolution can
be slow; every subsequent step is fast — single-flight + cached content
absorbing the upstream flake.

### Run #11 (2026-08-11) — **FULL PASS AGAIN ✅** (2 consecutive)

Same result as run10 — all 7 views green, `play_popular` ✅ again.
Two consecutive full passes satisfy #224's acceptance criterion
(2+ consecutive runs); the flake that took down run7 (37s cold
seasons) and run8 (502 → 404 detail) is closed.

### Run #12 (2026-08-14) — **FULL PASS ✅** (3 consecutive)

All 7 views green a third time, confirming the runner + #224 stack is
stable before the hunt for remaining unfilled fields began.

## Field-coverage sweep (2026-08-14, wire level)

Bulk scan of detail responses across 18 providers for unfilled fields
(Overview/Year/Genres/People). Findings and their disposition:

- **bambooua — description blank for EVERY title (FIXED, #226)**. The
  upstream emits standard JSON-LD keys (`@context`/`@graph`);
  `_JSONModel` had plain `graph`/`context` fields with no alias, so
  pydantic silently ignored them → `graph=[]` → description (and
  JSON-LD title) lost. Fixtures already carried `@graph` but no test
  asserted the description, so the whole-provider regression went
  unnoticed. Fix: `ConfigDict(populate_by_name=True)` + `Field(alias=
  "@graph")`/`Field(alias="@context")`. Live: desc_len 294-295 (was 0).
  Commits `6ec897f`, `5516a49`.
- **bambooua — year None for every title (FIXED, #226)**. Same JSON-LD
  carries `datePublished`; the model never parsed it. Now surfaced via
  the first 4 digits. Live: «Ти - моє серденько» year=2025.
- **animeon `{moved, redirectTo}` — NOT a bug.** The bare-id redirect
  IS handled (`_load_content_info` resolves the slug, covered by
  existing redirect fixtures). The empty description on «Зоряні Війни:
  Видіння» is an upstream data gap: the canonical API object has
  `description: None` and the page carries only the generic site
  description.
- **hentaiukr `#about` empty — expected.** Only an 18+ stub exists on
  those pages; nothing to parse.

## Field-coverage sweep round 2 (2026-08-14, wire level)

Second bulk scan (browse first section × 6 items per provider, checking
description/year/people). Providers with data on the page but nothing
parsed — each fixed via TDD + live-verified:

- **serialno — no year, empty People rail (FIXED, #227)**. The DLE
  `.flist` block carries `<span>Рік:</span>`, `<span>Режисер:</span>`,
  `<span>В ролях:</span>` rows with real data, never read. New
  `_parse_fmeta()` extracts the year and Person entries (ids
  `serialno:<name>`, round-trippable through /Persons). Live: «1670»
  year=2023 people=8, «Хроніки кращих часів» year=2004 people=9.
  Commit `95cc9c7`.
- **uaflix — no year/genres/people (FIXED, #228)**. The page's
  schema.org itemprop metadata (`dateCreated`/`genre`/`actor`/
  `director` — `<meta>` for serials, `<span>` for movies) was never
  read. New `_parse_itemprop_meta()` handles both shapes and
  comma-joined director lists. Live: «Останній дім» year=2026
  genres=3 people=6, «Супергірл» year=2026 people=6. Commit
  `cfce153`.
- **animeon `description: None` — upstream data gap, NOT a bug.** The
  canonical API object for «Зоряні Війни: Видіння» (and Naruto/One
  Piece/Attack on Titan probes) carries `description: None` and the
  HTML has only the generic site description. The bare-id redirect IS
  handled (`_load_content_info` resolves the slug; redirect fixtures
  exist).
- **unimay `actors` — upstream data gap.** The API exposes an `actors`
  field but returns `[]` for the probed releases; nothing to parse.
- **simpsonsuatv — NOT a bug (re-checked).** The earlier 404 was an
  artifact of my probe (no follow-redirects): `content()` resolves
  the `.html` fallback (301 → canonical season page) fine. The page
  has no year/people data at all — upstream data gap. The «Президент
  Кертіс» user report is separately tracked as B25/#215.
- **unimay/coaninet/doramyworld/eneyida/kinovezha — upstream data
  gaps.** Probed live pages/APIs: no actor/director rows and no
  datePublished to parse (eneyida/kinovezha pages carry no year at
  all). Not parser bugs.

## Field-coverage sweep round 3 (2026-08-14, wire level)

- **uakino — empty People rail (FIXED, #229)**. The `.fi-item` block
  carries Режисер/Актори rows with real `<a>` links, but content()
  only read Рік/Жанр/Країна. Added the two branches (Person ids
  `uakino:<name>`). Live: «Тихий притулок» actors=6 directors=1,
  «Сусіди зверху» actors=6 directors=1. Commit `cf31523`.
- **uakino + serialno — empty Genres row (FIXED, #230)**. Both
  providers parsed the Жанр data but never passed it to
  ContentResponse (uakino kept `tags` for the anime/movie
  classification only; serialno's `_parse_fmeta` skipped the row).
  Live: serialno genres=['Комедія'], uakino
  genres=['Детективи','Трилери','Жахи']. Commit `dd5b277`.
- **uakino — no rating badge (FIXED, #231)**. The label-less `.fi-item`
  row carries the IMDb-style `<score>/<votes>` (8.0/1118360 for Дюна)
  but the empty label hit the loop's continue guard. Parse it and wire
  `rating` → CommunityRating (#222 pattern). Live: «Всередині
  скандалу» 6.8, «Тихий притулок» 3.4, «Сусіди зверху» 7.8. Commit
  `b549a94`.
- **animeon — no year/genres (FIXED, #232)**. releaseDate is a bare
  year (`"2026"`/`"2002"`) and `strptime('%Y-%m-%d')` ValueError'd →
  year always None; the `genres[]` (nameUa) list was never read. Parse
  the first 4-digit year and map nameUa. Live: «Ми з тобою» year=2026
  genres=4, «Військова історія» year=2026 genres=4. Commit `e2e80cd`.

## Field-coverage sweep round 4 (2026-08-14, wire level)

- **detail loses year/genres for search-found groups (FIXED, #233)**.
  The `_content_dto` fallbacks #219/#220 (`_year_for_group` /
  `_genres_for_group`) only read the HOME SNAPSHOT cards — but a
  search-found group usually lives only in the shared resolution map
  (`register_search_groups`), so its detail dropped the year/genres
  its own search card surfaced. Wire repro: search «тихий» → «Тихий
  притулок» card year=2016, but its detail showed Year=None/Genres=[]
  (uakino is the group winner; its content page lacks the meta block
  while its search card carries year+genres). Fix: the fallbacks now
  also read the group's resolution-map cards (`resolve_group`), first
  non-empty wins. Live: detail `g2:59c2427fc3a49420` → Year 2016 +
  Genres [Детективи, Трилери, Жахи]. TDD red→green
  (`test_search_detail_falls_back_to_group_card_metadata`), 1014
  passed. Commit `d0f9043`.
- **search-only groups still miss genres (FOLLOW-UP, partially closed
  by #236)**: a group found ONLY via search (not in the home snapshot)
  — e.g. «Наруто» — used to render empty Genres/People because search
  cards carry no genres AND the first-seen provider's `content()`
  silently returned empty metadata. #236 revealed the second half was
  uakino's own bug (template renamed its meta rows to `fi-item-s`);
  with that fixed, uakino-first search groups now surface Year+Genres+
  Rating+People from `content()`. What REMAINS: a group whose FIRST
  provider is metadata-poor while a sibling is rich (e.g. an animeon
  sibling with genres=[Бойовик, Пригоди, …]) — the first-seen
  provider is still the only one asked. Closing THAT needs a "resolve
  the first provider with real metadata" (or merge-across-providers)
  change to `resolve_group_content` — deliberate, larger change.
- **uakino detail loses ALL metadata — upstream renamed `fi-item` rows
  to `fi-item-s` (FIXED, #236)**. The uakino template renamed its
  metadata rows from `fi-item clearfix` to `fi-item-s clearfix` (live
  on anime-series pages), so the parser's `div.fi-item` selector
  matched NOTHING on new-template pages: year/genres/rating/people all
  silently went empty. Unit fixtures still carried the old class — the
  suite stayed green. Fix: selector matches both spellings. Live:
  Беймакс Year 2022/Rating 7.2/Genres 2/People 9 (was 0/0/0/0);
  Наруто Year 2002/Rating 8.4/Genres 2/People 8. TDD red→green
  (`test_uakino_content_parses_metadata_from_suffixed_fi_item_rows`),
  1018 passed. Commit `865f5e2`.
- **klontv — Year/Genres blank when klontv wins the group (FIXED,
  #237)**. `content()` parsed only rating/cast from the JSON-LD; the
  page's `table-info__item` rows (`Рік:` link `/year/2000/`, `Жанр:`
  links `/dramy/`, `/boyovyky/`, …) were never read, so groups whose
  FIRST provider is klontv rendered Year=None/Genres=[] even when a
  sibling (uakino) carried the data. New `_table_info_year_genres()`
  parses the rows, excluding the section link (Серіали/Фільми — a
  section, not a genre). Live: «Тихий притулок» (g2:59c2427fc3a49420)
  → Year 2016 + Genres [Детективи, Жахи, Трилери] (was blank),
  Rating 3.4, People 17.  TDD red→green
  (`test_klontv_content_parses_year_and_genres`), 1019 passed.
  Commit `10b38fe`.
- **uakino — section name leaks into Genres row (FIXED, #238)**. The
  upstream Жанр row opens with the SECTION name (`'Серіали , Драма ,
  Пригоди , Фантастика'` on series pages) and content()'s
  `split(",")` kept it as a genre — series details rendered
  «Серіали» in the Genres row (same class as #237). Fix: filter the
  UAKINO_SECTIONS titles out of the parsed tags. Live: «Дюна 1
  сезон» (g2:c3200c673b15493a) → Genres [Драма, Пригоди,
  Фантастика] (was [Серіали, Драма, …]). TDD red→green
  (`test_uakino_content_excludes_section_from_genres`), 1020 passed.
  Commit `f9479b7`.
- **ufdub episodes stream 404 while PlaybackInfo answers 200 (FIXED,
  #235)**. ufdub `stream()` returns the `VIDEOS.php` gateway URL;
  movies 302 to `api.ufdub.com` (same registrable domain — admitted by
  the dot-boundary CDN check) but series episodes 302 to
  `dl.dropboxusercontent.com` — a FOREIGN registrable domain the D7
  SSRF posture rejected, failing the byte proxy closed to 404. Wire
  repro: PlaybackInfo on `ufdub:anime-6-komori-san-…:s1e1` → 200
  (Container=mp4), `/Videos/…/stream` → 404; log showed the 302 to
  Dropbox then an abort. Fix: `StreamResponse` gained `allowed_domains`
  (provider-sanctioned registrable domains beyond the URL's own host),
  ufdub declares `dropboxusercontent.com`, and
  `_stream_target_allowed`/`_open_upstream`/`_fetch_manifest` + the
  segment memo thread it through — undeclared foreign hosts still fail
  closed. TDD red→green
  (`test_stream_follows_redirect_to_provider_allowed_cdn`), 1017
  passed. Live: episode stream 206 Partial Content via Dropbox (was
  404). Commit `a135121`.

## Running the suite (2026-08-10, codified)

> Wire-cheat-sheet: the episode rail is
> `GET /Shows/{group}/Episodes?seasonId={group}:S{n}` — `seasonId`
> goes in the QUERY, NOT the path (`/Shows/{season}/Episodes` returns
> an empty rail because `season_id` defaults to None). A "episodes:
> 0" probe result is nearly always this caller bug, not a backend
> one.

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

## Resume observation step (spec #247 / ticket #251, manual sweep)

One manual checklist line for the on-device pass — deliberately NOT a
`steps.yaml` step definition (it needs an app kill + relaunch mid-flow):

- [ ] Play a movie/series to ~40% on the device, then kill the app
      (not just stop playback) and relaunch it: «Продовжити перегляд»
      shows the item with a proportionally-filled resume bar
      (`/Items/Resume` serves `PlaybackPositionTicks` + `RunTimeTicks`
      — wire-verified on the backend, #248–#250).
- [ ] Watch the same item to the end (≥95% of runtime): it leaves
      «Продовжити перегляд» and no longer feeds «Далі» (`/Shows/
      NextUp`) — finished-marking, #249.
- [ ] Restart the backend host process while an item is in-progress:
      the row still shows it after the restart (disk-backed store, #248;
      the state file is `~/.cache/cs-uk-api/playback.json` unless
      `CS_UK_RESUME_PATH` overrides it).
- [ ] Recommendations (spec #252): watch an item (or search a couple of
      titles), return home, and the personalized rows appear —
      «Рекомендовано для тебе» and «Схоже на <title>» — positioned
      after «Популярні зараз», and opening a card works like any other
      row. They are just new home-row kinds served through the existing
      view mechanism (zero client changes).

- [ ] Home composition (spec #263): the sidebar's Views grid now shows
      «Нещодавно додані: Фільми» and «Нещодавно додані: Серіали»
      (replacing «Новинки»), plus up to six genre rails (Пригоди,
      Фентезі, …). Open a card from each of the two recent rows and
      from one genre rail — it resolves like any other card (wire
      -verified live 2026-08-14; `steps.yaml` drives `recent_movie` /
      `recent_series`). **Tap coordinates for the two recent rows and
      the shifted grid (B8) are ESTIMATES — re-calibrate on-device
      before the next sweep run** (`view_recent_series_x` at the
      second grid slot; `view_popular_x` moved to the third).

- [ ] Netflix parity round 2 (spec #267): on a detail screen, the
      «Схожі» shelf is populated with ranked cards for an item with
      warm content profiles (a genre-less item with signal is no longer
      empty — profile scorer, #268; wire-verified on the backend).
- [ ] Restart the backend host process: the first facade open after the
      restart answers INSTANTLY from the persisted home snapshot
      (`home-snapshot.json` next to `playback.json`, knob
      `CS_UK_SNAPSHOT_PATH`) while the rebuild heals in the background
      (#269; wire-verified cold-start serve, no fan-out).
- [ ] Watch a series episode, return home: «Нові серії» appears at
      position 3 of the home (after the two recent rows, before
      «Популярні зараз») and its card opens like any other row (#270,
      `new_episodes` view; wire-verified).

- [ ] Netflix parity round 3 (spec #272): on a detail screen, tap an
      actor on the People rail — the person page lists their movies and
      series (split by the client's `includeItemTypes`; the facade
      matches `PersonIds` against the profile store, wire-verified on
      the backend). Portraits stay placeholder (accepted).
- [ ] Watch any item to the end (≥95% of runtime): it leaves
      «Продовжити перегляд» but «Нещодавно переглянуто» still shows it
      at home position 4 (after «Нові серії», before «Популярні
      зараз»), and tapping the card opens the item (#272; wire-
      verified finished-included row + view).

- [x] User state (spec #257, verified 2026-08-14): on a detail screen tap
      the heart — it lights and stays lit after an app relaunch (toggle
      answers UserDataResult, mark persists in `user-state.json`, and the
      detail DTO re-reads it on refetch → button shows «Remove favorites»);
      from a card's context menu (KEYCODE_MENU) mark an item played — the
      checkmark badge appears in the card's top-right corner. **The Remote
      and Live TV tabs could NOT be verified: Switchfin 0.9.3 exposes no
      entry point for them** (sidebar has Downloads where the old doc said
      Remote; no Live TV anywhere reachable). The graceful-empty routes are
      wire-tested; the client has never called them in any session.
- [x] User state (spec #257, AC4, verified 2026-08-14): the Download button
      **FAILS** — it calls `GET /Items/{id}/Download?api_key=…` → **404**
      (no such route; only `/Videos/{id}/stream` exists, which answers 200).
      The app records `status: Failed / http status 404` in
      `downloads/index.json`. Follow-up: **ticket #296** (add the
      `/Items/{id}/Download` route).

Backend-side verification of the same behaviour (no device needed):
`POST /Sessions/Playing/Stopped` with `PositionTicks`+`RunTimeTicks`
→ `GET /Users/{user}/Items/Resume` and `GET /Shows/NextUp` (see the
`test_resume_store.py` / `test_jellyfin_detail.py` #248–#250 tests);
`POST/DELETE /Users/{user}/FavoriteItems/{id}` and `/PlayedItems/{id}`
→ UserDataResult, and `UserData` on card/detail/episode DTOs (see
`test_user_state.py` / `test_jellyfin_detail.py` #257 tests).
