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

## Open questions (for the next session)

- DONE (2026-08-10): `Adb.back()` + `phase: nav` steps wired into the runner;
  `seasons_tab` removed from the series flow (B7 — the episode-row tap
  auto-plays); warmup phase added (B1). See `git log` on this branch.
- **Fix B10**: drop the `/Images/Primary` expect from `open_first_card_*`
  (the real client never fires it on the detail screen) and give the play
  step a dynamic play-button locate + retry instead of the fixed (825, 470).
- **Verify B11** (library `/Items` re-fetch loop) with a controlled cold-open
  reproduction; if real, ticket it as an app bug.
- **Run the full 37-step suite again** after the B10 fixes and check the
  verdict, snapshots, capture fixture, and logcat filters end-to-end.
