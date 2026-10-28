# Custom PS4 Switchfin build

The PS4 client is `dragonflylee/switchfin` (C++ / borealis). Four of the
viewer-facing problems can only be fixed in the client, so this directory
carries the patches and the CI workflow that builds a PKG on GitHub Actions
(the workstation has no docker/sudo for the pacbrew/OpenOrbis container).

Build: push a `client-build/**` branch — `.github/workflows/ps4-client-build.yml`
checks out the pinned upstream commit, applies these patches, builds with
`xfangfang/pacbrew:250221` and uploads `Switchfin-PS4-custom-<run>.pkg`.

Install: copy the PKG to the console over GoldHEN FTP, then install it from
the console's package installer. The stock 0.9.4 PKG is kept beside it for a
rollback.

## Patches

- `0001-dub-source-picker.patch` — the Jellyfin facade lists one
  `MediaSource` per translation (spec #276 of the backend), but the stock
  player silently played `MediaSources[0]` and the detail-screen «Версія»
  picker exists for movies only, so a series viewer could never pick the dub.
  The patch shows an «Озвучка» dropdown before playback when PlaybackInfo
  returns several sources, labels rows from the audio stream `DisplayTitle`,
  and keeps the picked dub for the next episodes of the same player session.
  `Source.DisplayTitle` is added to the client's JSON model (it was dropped).
- `0002-ukrainian-search-keyboard.patch` — the on-screen keyboard was
  `A-Z` + digits only, and the PS4 system IME has no Ukrainian layout
  (OpenOrbis exposes Russian only), so Ukrainian could not be typed at all.
  The patch puts the Ukrainian alphabet first, adds space/apostrophe, and
  makes Delete erase a whole UTF-8 code point (it popped one byte, leaving
  half a Cyrillic letter in the query).
- `0003-preserve-focus-on-reload.patch` — every `reloadData()` re-anchored a
  grid at cell 0, so asynchronous data refreshes made the search
  suggestions, home rows and season/episode lists jump back to the start
  while they were being scrolled. The patch restores the focused cell index
  when the reloaded data still covers it; the search tab also stops blanking
  the results grid with a skeleton on every keystroke.
- `0004-raise-request-timeout.patch` — the client's default HTTP timeout is
  **3 s**, while the facade's first home load fetches the "latest" shelf of
  every library in parallel (19 rows) and takes 6–8 s on a cold provider
  cache. The stock client therefore shows "Timeout was reached" toasts and
  leaves home rows as empty skeletons for viewers who never set an explicit
  timeout (a fresh PS4 config.json has none). The patch raises the default
  to 10 s — an existing dropdown option, so the setting stays consistent.
  Reproduced and fixed on the Android build with the same source: with the
  default 3 s the home screen stayed empty, with `request_timeout` raised the
  rows and posters loaded.
