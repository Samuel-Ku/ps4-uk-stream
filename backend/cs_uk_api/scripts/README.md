# Backend scripts

Operational scripts living next to the API package
(`backend/cs_uk_api/scripts/`). All run from the `backend/` directory
with the venv active; exit code 0 means OK.

## `gate.sh <provider> [query] | gate.sh --all`

Per-provider live gate (issue #30, spec §7.1): boots the API on
`127.0.0.1:${PORT:-8002}`, then runs **search → content → stream →
mpv plays 1 frame** and reports GATE PASS/FAIL per provider.

- `query` defaults to `Дюна`; catalogs that rotated their listings
  need a content-specific query (e.g. `фільм`, `школярки`, `квітка`).
- On mpv failure the gate downloads the player HTML and scans it for
  JS-generation markers (`eval(`, `Function(`, `atob(`, `obfuscated`);
  a "not portable" verdict is issued only on real marker evidence.
- On success the resolved stream is ffprobed and anything that is not
  H.264 is flagged `ps4-soft-decode-risk` (mpv on PS4 decodes in
  software).
- Exit codes: 0 = at least one provider passed; 1 = GATE FAIL; 2 =
  missing dependency or bad usage. Requires `uvicorn`, `python3`,
  `curl`, `mpv`, `timeout`, `ffprobe` on PATH.

## `refresh_uakino.py [--close]`

Detached external probe (issue #51). uakino.best's Cloudflare gate is a
silent per-request JS check — no cookie exists to persist — so the
"refresher" is a host-level health check: it boots a fresh headless
Chromium session and probes a known content page and the playlists ajax
endpoint, printing `OK`/`FAIL` lines.

- `--close` shuts the session down afterwards; without it the browser
  stays open (harmless — the probe process exits anyway).
- Requires `playwright` and a system Chromium
  (`UAKINO_CHROMIUM` env var to override `/usr/bin/chromium`).
- Detached: this script does **not** share state with the API process.
  The API's in-process warm + 5-minute heartbeat (issues #193/#195)
  covers liveness while the server runs; this script answers a different
  question — can a fresh session warm from zero on this host?

See `docs/research/uakino-bypass-2026-08-02.md` for the investigation
behind the browser-session architecture.

## `doc_drift_guard.py [--root PATH] [--verbose]`

Every backticked reference in the living docs must resolve against the
tracked codebase: nothing used to fail when code moved and a doc kept
naming the dead symbol as load-bearing fact. The guard reports each
unresolved span with file and line; its allowlist holds *documented*
non-existence only, one reason per entry.

- `--root` defaults to the repo root; `--verbose` also prints the
  allowlist with its reasons. Also enforced by
  `cs_uk_api/tests/test_doc_drift_guard.py`, so the release gate runs it
  on every push.
- A cited commit SHA is checked against the object database, so a
  SHALLOW checkout cannot verify it — the gate fetches full history
  (`fetch-depth: 0`) rather than allowlisting the commits the docs cite.
