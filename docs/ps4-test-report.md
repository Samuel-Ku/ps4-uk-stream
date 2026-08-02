# PS4 Test Report

> Fill this in on the actual PS4 (FW 11.00 + GoldHEN) when you do the
> on-console test. This is the final step for the Definition of Done.

**Date:** YYYY-MM-DD
**Firmware:** 11.00
**HEN:** GoldHEN <version>
**PKG:** PPLA00001 v3.7-uk-stream-<git-sha>
**Backend:** http://192.168.2.223:8000, commit <git-sha>
**PS4 IP:** 192.168.2.105 (FTP port 2121, GoldHEN)

## Build verification (run on the Linux host before installing)

- [ ] `./pplay-fork/scripts/build-ps4-docker.sh` exits 0
- [ ] `pplay-fork/build/PPLA00001.pkg` exists, size > 1 MB
- [ ] `head -c 4 pplay-fork/build/PPLA00001.pkg | xxd` → `7f 43 4e 54`
      (`\x7FCNT` PKG magic)
- [ ] `pplay-fork/build/eboot.bin` exists, `file` reports
      `ELF 64-bit LSB executable, x86-64, ...FreeBSD`

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
it to the Linux host's LAN IP: `http://192.168.2.223:8000`. Save.

### Copy data/ to the PS4 internal HDD

Per upstream pPlay README, the PS4 build expects a `data/` folder on
the internal HDD:

```bash
lftp -c "open -u anonymous, ftp://192.168.2.105; \
  mirror -R pplay-fork/data /user/data/pplay/"
```

## Checklist

### Cold start

- [ ] App launches, main menu visible
- [ ] "Каталог UA" present in main menu (top section) and focusable
- [ ] "Пошук UA" present in main menu (top section) and focusable
      (added in Task 18)

### Browse path: Sections → Results → Content → Play

- [ ] "Каталог UA" opens a two-column Sections screen
- [ ] Provider list (left) is non-empty (>= 3 providers visible)
- [ ] D-pad Right moves the cursor to the right column
- [ ] D-pad Up/Down moves within each column with wrap-around
- [ ] A on a section opens ScreenResults with posters
- [ ] Posters load (poster URL resolves through the backend's
      `/api/poster` route)
- [ ] A on a result opens ScreenContent (synopsis + seasons + episodes)
- [ ] D-pad Left/Right cycles seasons
- [ ] D-pad Up/Down cycles episodes
- [ ] A on an episode hands off to mpv and the stream starts

### Search path: Search → Results → Content → Play

- [ ] "Пошук UA" opens the on-screen keyboard
- [ ] Cyrillic characters are selectable and append to the query
      (try at least one letter from each row of the Ukrainian alphabet)
- [ ] Y inserts a space, X backspaces, A on `OK`/Start submits
- [ ] Empty query does not fire a search (status stays "Введіть запит")
- [ ] Search results render with posters
- [ ] From results: A → ScreenContent → A → mpv plays (same as browse)

### Translation levels

Two providers expose per-episode dub/sub selection. On a content screen
with `translations_level = "episode"`:

- [ ] `Y` (Fire3) cycles the episode translation (only when the content
      has > 1 episode-level translation)
- [ ] The currently-selected translation tag is visible in the episode
      row, e.g. `E3 · Назва · [Українська]`
- [ ] A plays with the selected translation; switching and re-pressing
      A actually swaps the stream

Providers with `translations_level = "episode"` (verify at least one):

- [ ] animeua
- [ ] anitubeinua
- [ ] animeon
- [ ] coaninet (some content)
- [ ] kinotron (some content)

### Provider coverage

Mark at least 5 of the 18 v2 providers with PASS/FAIL on a search →
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

### Performance & regressions

- [ ] Section screen renders in < 1 s after backend `GET /api/sections`
- [ ] Search returns in < 12 s total (backend budget is 12 s for the
      cross-provider search; verify by timing on the backend logs)
- [ ] mpv plays the first frame in < 3 s after A on an episode
- [ ] No app crash on B (back) from any catalog screen
- [ ] No freeze when the backend host is unreachable
      (test by pointing OPT_CATALOG_URL at a black-hole IP, then open
      "Каталог UA" — should show "Backend недоступний", not freeze)
- [ ] PS4 controller input remains responsive throughout

## Notes

<free-form observations, crashes, FPS, sync issues>

## Verdict

- [ ] **PASS** -- Definition of Done met
- [ ] **FAIL** -- see notes above
