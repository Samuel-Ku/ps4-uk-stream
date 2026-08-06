# PS4 Test Report

> Fill this in on the actual PS4 (FW 11.00 + GoldHEN) when you do the
> on-console test. This is the final step for the Definition of Done.
>
> Sections marked `[harness]` can be marked now from the agent host
> (CLI, tests, build pipeline); sections marked `[console]` MUST be
> filled on the PS4.

**Date:** YYYY-MM-DD
**Firmware:** 11.00
**HEN:** GoldHEN (version to fill on the console)
**PKG:** PPLA00001 v3.7-uk-stream-42e565a (7,012,352 bytes)
**Backend:** <http://192.168.2.166:8765> (port 8000 is the user's own app on dhcpcd)
**PS4 IP:** 192.168.2.105 (FTP port 2121, GoldHEN)

## Bug #18 verification (issue #97)

After merging #96 (dynamic gp4 enumeration), `pplay_pkg` should emit
a 33 MB ± 1 MB PKG with the same 5 PS4 system files in its file
table as upstream pPlay 3.8 (icon0.png, param.sfo, playgo-chunk.dat,
playgo-chunk.sha, playgo-manifest.xml).

- [ ] PKG size is 33 MB ± 1 MB (was 7 MB on 42e565a) — re-run
      `bash pplay-fork/scripts/build-ps4-docker.sh` after the #96
      merge and read `pplay-fork/build/bug-18-verification.txt`
- [ ] PKG SHA-256: *see `pplay-fork/build/bug-18-verification.txt`*
      (copied here on the next build):
      `__FILL_IN_AFTER_DOCKER_BUILD__`
- [ ] eboot.bin magic `4f 15 3d 1d` and paid `0x3800000000000011`
      still present (regression check)

> The verification is run inside the `ps4-uk-build` container by
> `pplay-fork/scripts/verify-bug18.sh` (step `[6/5]` of the in-container
> pipeline). On a successful run it writes
> `pplay-fork/build/bug-18-verification.txt` — read that file on the
> host and paste the `pkg_sha256` value above.

## Build verification (run on the Linux host before installing) `[harness]`

- [x] `./pplay-fork/scripts/build-ps4-docker.sh` exits 0
      (verified on commit 42e565a: PKG produced, validations all pass)
- [x] `pplay-fork/build/PPLA00001.pkg` exists, size > 1 MB
      (verified on 42e565a: **7,012,352 bytes**, 7.01 MB — pre-#95/#96;
      post-#96 this should be ~33 MB, see "Bug #18 verification" above)
- [x] `head -c 4 pplay-fork/build/PPLA00001.pkg | xxd` → `7f 43 4e 54`
      (`\x7FCNT` PKG magic; verified on 42e565a)
- [x] `pplay-fork/build/eboot.bin` exists, magic `4f 15 3d 1d` (SCE
      SELF wrapper from create-fself, fake-signed with
      `paid=0x3800000000000011`; verified at offset 864 of the SELF
      on 42e565a)

## Agent-host verification (Linux host, before installing) `[harness]`

These items do NOT require a PS4. They lock the build pipeline and
the catalog-state unit suite so the on-console test inherits a known-
good binary.

- [x] `cmake --build pplay-fork/tests/standalone-catalog/build` builds
      every test binary (no link errors)
- [x] `ctest --test-dir pplay-fork/tests/standalone-catalog/build` is
      100% green (verified: **17/17** on commit 42e565a, after the
      `std::filesystem` → POSIX refactor in PosterCache.cpp)
- [x] Backend test suite is 100% green (verified: **632 passed**
      on commit 141d574, feat/v2-providers #102–#105)
- [x] `pplay-fork/src/catalog/ScreenHome.*` and `ScreenContent.*`
      compile clean under `-Wall -Wextra` (no warnings introduced by
      the v3 work; the only remaining warnings are the pre-existing
      `-Wc++17-extensions` for nested `namespace cs { namespace ... }`,
      which don't fail the build)

## Setup commands used

### Transfer the PKG (run on the Linux host)

```bash
lftp -c "open -u anonymous, ftp://192.168.2.105; \
  put pplay-fork/build/PPLA00001.pkg \
      /user/data/GoldHEN/plugins/PPLA00001.pkg"
```

(Or copy to USB and install via the debug-menu package installer.)

### Configure the backend URL

In the PS4 main menu of pPlay, open Settings -> "Адреса сервера" and set
it to the Linux host's LAN IP and the port the ps4-uk-stream backend is
running on. The host's IP is `192.168.2.166`; the port the agent started
the backend on is **8765** (the documented 8000 is occupied by the user's
own app on the same host, so the agent had to move to 8765). Use
`<http://192.168.2.166:8765>`. Save.

### Copy data/ to the PS4 internal HDD

Per upstream pPlay README, the PS4 build expects a `data/` folder on
the internal HDD:

```bash
lftp -c "open -u anonymous, ftp://192.168.2.105; \
  mirror -R pplay-fork/data /user/data/pplay/"
```

## V2 checklist (Task 20 acceptance)

### Cold start `[console]`

- [ ] App launches, main menu visible
- [ ] "Каталог UA" present in main menu (top section) and focusable
- [ ] "Пошук UA" present in main menu (top section) and focusable
      (added in Task 18)

### Browse path: Sections → Results → Content → Play `[console]`

- [ ] "Каталог UA" opens a two-column Sections screen
- [ ] Provider list (left) is non-empty (>= 3 providers visible)
- [ ] D-pad Right moves the cursor to the right column
- [ ] D-pad Up/Down moves within each column with wrap-around
- [ ] X (Cross) on a section opens ScreenResults with posters
- [ ] Posters load (poster URL resolves through the backend's
      `/api/poster` route)
- [ ] X (Cross) on a result opens ScreenContent (synopsis + seasons + episodes)
- [ ] D-pad Left/Right cycles seasons; R1/L1 page results
- [ ] D-pad Up/Down cycles episodes
- [ ] X (Cross) on an episode hands off to mpv and the stream starts

### Search path: Search → Results → Content → Play `[console]`

- [ ] "Пошук UA" opens the on-screen keyboard
- [ ] Cyrillic characters are selectable and append to the query
      (try at least one letter from each row of the Ukrainian alphabet)
- [ ] Triangle inserts a space, Square backspaces, X (Cross) on `OK`/Options submits
- [ ] Empty query does not fire a search (status stays "Введіть запит")
- [ ] Search results render with posters
- [ ] From results: X (Cross) → ScreenContent → X (Cross) → mpv plays (same as browse)

### Translation levels `[console]`

Two providers expose per-episode dub/sub selection. On a content screen
with `translations_level = "episode"`:

- [ ] Triangle (Fire3) cycles the episode translation (only when the content
      has > 1 episode-level translation)
- [ ] The currently-selected translation tag is visible in the episode
      row, e.g. `E3 · Назва · [Українська]`
- [ ] X (Cross) plays with the selected translation; switching and
      re-pressing X (Cross) actually swaps the stream

### Provider coverage `[console]`

Mark at least 5 of the 19 v2 providers with PASS/FAIL on a search →
play smoke. One row per provider:

| #  | Provider      | Browse | Search | Movie | Series | Anime |
|----|---------------|--------|--------|-------|--------|-------|
| 1  | uakino        |        |        |       |        | n/a   |
| 2  | ufdub         |        |        |       |        | n/a   |
| 3  | uaflix        |        |        |       |        | n/a   |
| 4  | unimay        |        |        |       |        | n/a   |
| 5  | kinotron      |        |        |       |        |       |
| 6  | cikavaideya   |        |        |       |        | n/a   |
| 7  | hentaiukr     |        |        | n/a   | n/a    |       |
| 8  | bambooua      |        |        |       |        | n/a   |
| 9  | kinovezha     |        |        |       |        | n/a   |
| 10 | animeua       |        |        | n/a   | n/a    |       |
| 11 | coaninet      |        |        |       |        | n/a   |
| 12 | eneyida       |        |        |       |        | n/a   |
| 13 | klontv        |        |        |       |        | n/a   |
| 14 | serialno      |        |        |       |        | n/a   |
| 15 | doramyworld   |        |        |       |        | n/a   |
| 16 | uaserialspro  |        |        |       |        | n/a   |
| 17 | anitubeinua   |        |        | n/a   | n/a    |       |
| 18 | animeon       |        |        | n/a   | n/a    |       |
| 19 | simpsonsuatv  |        |        | n/a   | n/a    |       |

Use ✅ / ❌ / n/a. If ❌, capture the failure mode in `## Notes` below.

### V2 performance & regressions `[console]`

- [ ] Section screen renders in < 1 s after backend `GET /api/sections`
- [ ] Search returns in < 12 s total (backend budget is 12 s for the
      cross-provider search; verify by timing on the backend logs)
- [ ] mpv plays the first frame in < 3 s after X (Cross) on an episode
- [ ] No app crash on Circle (back) from any catalog screen
- [ ] No freeze when the backend host is unreachable
      (test by pointing OPT_CATALOG_URL at a black-hole IP, then open
      "Каталог UA" — should show "Backend недоступний", not freeze)
- [ ] PS4 controller input remains responsive throughout

## V3 checklist (M3 acceptance)

### Home (issue #61) `[console]`

- [ ] Single "Каталог UA" menu entry opens the Home screen
- [ ] Loupe pill at the top-right opens ScreenSearch
- [ ] All rows from `/api/home` render with readable 10-foot typography:
      "Новинки", «Популярні зараз» (when present), five type rows
- [ ] D-pad Up/Down moves between rows; Left/Right moves within a row
- [ ] X (Cross) on a card opens ScreenContent for the right groupKey
- [ ] X (Cross) on "Ще →" opens ScreenResults for the row's type
- [ ] Empty / missing rows are hidden (no empty headers)
- [ ] The screen scrolls vertically when focus would push past the
      bottom edge

### Resume row (issue #66) `[console]`

- [ ] The "Продовжити перегляд" row appears first when the user has
      at least one unfinished resume entry
- [ ] Each card shows "provider · MM:SS" with a green outline
- [ ] Selecting a card opens ScreenContent for the remembered groupKey
- [ ] §Selecting a card does NOT auto-play; the user lands on the
      details screen and must press X to play
- [ ] After watching, the entry position updates; on completion
      (>= 95% per CatalogState::isFinished) the entry drops from the
      row on the next visit
- [ ] The row is empty (and therefore hidden) on a fresh install

### Source/dub memory pre-focus (issue #67) `[console]`

- [ ] On a series details screen, the chip strip pre-focuses on the
      remembered provider
- [ ] A remembered provider that is Down (according to the latest
      `/api/providers` snapshot) is skipped, and the next healthy
      provider is selected instead
- [ ] The content-level translation (when translations_level=content)
      is pre-selected to the remembered label
- [ ] Movies never have a memory entry (the chip strip first-render
      focus is the first healthy source)

### Source chips + grayed-down sources (issue #73) `[console]`

- [ ] On a details screen, the chip strip shows one chip per provider
      that contributed to the group
- [ ] A provider known to be Down (§ via /api/providers snapshot) is
      rendered with a "● Down" hint and is not selectable
- [ ] A provider known to be Degraded is rendered with a "⚠" hint
      and IS selectable
- [ ] Switching chips refetches `/api/content/{groupKey}?source=<p>`
      and the screen renders the new content

### Resume banner on details (issue #72) `[console]`

- [ ] When a details screen is opened for a group with a live resume
      entry, the "▶ Поновити з MM:SS" banner appears below the chips
- [ ] The banner is hidden when:
      - the entry has finished (>= 95%) — even mid-session
      - the entry has no position yet (positionSec == 0)
      - the entry has an unknown duration but a position is shown
        (we don't know it's finished, so we offer the position)

### Black-hole backend (issue #64) `[console]`

- [ ] Point OPT_CATALOG_URL at a black-hole IP and open "Каталог UA"
- [ ] The Home screen shows the inline error screen: "Сервер
      недоступний" + body + "Повторити" pill
- [ ] No freeze — the rest of the menu remains responsive
- [ ] X (Cross) on "Повторити" re-fires `/api/home`; if the backend
      is back, the screen renders normally

### Playback (issue #65, blocked by #36) `[console]`

- [ ] X (Cross) on an episode hands off to the player and the stream
      starts (or to the SceAvPlayer path if the spike lands)
- [ ] OSD shows/hides correctly; Triangle seeks
- [ ] Playback position is reported back to the catalog layer
      (resume hook works on PS4)
- [ ] At least 3 consecutive episodes play without a crash

## Notes

<free-form observations, crashes, FPS, sync issues>

## Verdict

- [ ] **PASS** -- Definition of Done met
- [ ] **FAIL** -- see notes above
