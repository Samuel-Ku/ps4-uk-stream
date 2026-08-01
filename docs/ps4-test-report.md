# PS4 Test Report

> Fill this in on the actual PS4 (FW 11.00 + GoldHEN) when you do the
> on-console test. This is the final step for the Definition of Done.

**Date:** YYYY-MM-DD
**Firmware:** 11.00
**HEN:** GoldHEN <version>
**PKG:** PPLA00001 v3.8-uk-stream-<git-sha>
**Backend:** http://192.168.2.223:8000, commit <git-sha>
**PS4 IP:** 192.168.2.105 (FTP port 2121, GoldHEN)

## Checklist

- [ ] App launches, main menu visible
- [ ] "Каталог UA" present and focusable
- [ ] Sections screen lists providers + sections; browse returns posters
- [ ] On-screen keyboard accepts Cyrillic input and submits a search
- [ ] Search submits and results render
- [ ] Posters load
- [ ] Movie plays via MPV (no sync glitches)
- [ ] Series: pick season -> pick episode -> plays
- [ ] Anime: episode-level translation chooser works (dub/sub)
- [ ] At least 3 different providers verified

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

## Notes

<free-form observations, crashes, FPS, sync issues>

## Verdict

- [ ] **PASS** -- Definition of Done met
- [ ] **FAIL** -- see notes above
