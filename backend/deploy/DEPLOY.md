# Deployment runbook — cs-uk-api on a standalone host

Step-by-step reproduction of the backend deployment (spec #298, ticket #304).
Follow this on a clean Linux host to stand up the backend without tribal
knowledge. The dev host's values are quoted inline as the reference; **every
host-specific value is in a table in §2** — edit those before enabling.

The three systemd units shipped beside this file are the whole deployment:

- `cs-uk-api.service` — the backend (uvicorn, LAN-only, port 8003).
- `cs-uk-api-drift.service` — one-shot nightly upstream-drift sweep.
- `cs-uk-api-drift.timer` — the timer that runs the drift sweep nightly.

## 0. Prerequisites (packages, not host-specific)

- Linux (systemd), Python ≥ 3.11 (the dev host uses 3.12).
- `git`, `python3-venv` (or equivalent), `pip`.
- A system Chromium binary for the uakino provider (default
  `/usr/bin/chromium`, override with `UAKINO_CHROMIUM`). uakino's
  Cloudflare gate only passes in-page `fetch()` calls, so the API runs a
  headless-browser session for that one provider (`playwright` is a pip
  dependency; the browser binary itself is the system Chromium).
- **Optional but recommended:**
  - `gh` CLI, authenticated as the operator (`gh auth login`, token scope
    `repo`) — required for the drift monitor to file GitHub issues. Without
    it the sweep still runs; issue filing is skipped (see §5).
  - `mpv`, `ffprobe`, `curl`, `timeout` — only for the per-provider live
    gate (`cs_uk_api/scripts/gate.sh <provider>`), not for serving.

## 1. Clone, venv, install

```bash
git clone https://github.com/Samuel-Ku/ps4-uk-stream.git
cd ps4-uk-stream/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` is the runtime dependency set (fastapi, uvicorn,
httpx, bs4, lxml, pydantic, cachetools, pycryptodome, playwright,
pillow). The dev extras (`pytest`, `ruff`, `mypy`, `respx`) are optional
— only needed to run the release gate on the host.

## 2. Host-specific values to edit (do this BEFORE enabling)

All three units carry dev-host paths. Edit each file in
`backend/deploy/` after cloning (or copy then edit in place):

| Unit | Value | Dev host (reference) | Change to |
| ---- | ----- | -------------------- | --------- |
| all | `User=` | `rorschach` | the operator user on the new host |
| all | `WorkingDirectory=` | `/home/rorschach/UA flims/ps4-uk-stream/backend` | repo path on the new host |
| `cs-uk-api.service` | `ExecStart=` venv/uvicorn path | `/home/rorschach/UA flims/ps4-uk-stream/backend/.venv/bin/uvicorn` | the venv's `bin/uvicorn` on the new host |
| `cs-uk-api-drift.service` | `ExecStart=` venv/python path | `/home/rorschach/UA flims/ps4-uk-stream/backend/.venv/bin/python` | the venv's `bin/python` on the new host |
| `cs-uk-api.service` | `--port` | `8003` | **keep 8003** (see §4) — do not move back to 8000 |

Notes:

- Paths with spaces must stay quoted (systemd splits `ExecStart=` on
  whitespace) — the dev path contains `UA flims/`.
- The units are LAN-only by design (ADR-0003: one host, one uvicorn
  process, no auth) — `--host 0.0.0.0` and the Jellyfin facade is
  accept-any. Do not expose them to a non-LAN interface.
- Runtime state (poster disk cache, resume `playback.json`, user-state
  `user-state.json`, drift report/state) defaults to `~/.cache/cs-uk-api/`
  of the `User=` account. Wipe or back up per the README's knobs table.

### Optional env knobs (uncomment in the unit to tune)

| Knob | Meaning |
| ---- | ------- |
| `CS_UK_CACHE_SEARCH` / `CS_UK_CACHE_CONTENT` / `CS_UK_CACHE_POSTER` | cache TTLs (s), defaults 300 / 1800 / 3600 |
| `CS_UK_POSTER_DISK_TTL` | poster disk-cache TTL (s), default 7 days |
| `CS_UK_RESUME_PATH` / `CS_UK_USER_STATE_PATH` | relocate the resume / user-state JSON files |
| `CS_UK_ROW_MAX_PAGES` | deep-rows depth (spec #305): max upstream browse pages fetched per home row beyond the snapshot when the client scrolls (default 5 ≈ 100 cards per row) |
| `UAKINO_CHROMIUM` | Chromium binary for the uakino browser session (default `/usr/bin/chromium`) |
| `CS_UK_LLM_BASE_URL` / `CS_UK_LLM_KEY` / `CS_UK_LLM_MODEL` | optional LLM taste-profile layer (all three set to activate; never commit the key) |
| `CS_UK_DRIFT_STATE` / `CS_UK_DRIFT_REPORT` | relocate the drift baseline/report files (default `~/.cache/cs-uk-api/drift-{state,report}.json`) |

## 3. Install and enable

```bash
cd ps4-uk-stream/backend
sudo cp deploy/cs-uk-api.service deploy/cs-uk-api-drift.service \
     deploy/cs-uk-api-drift.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cs-uk-api.service
sudo systemctl enable --now cs-uk-api-drift.timer
```

If the backend previously ran by hand on this host, stop the manual
uvicorn **before** `enable --now cs-uk-api.service` so the service can
bind 8003 without a conflict:

```bash
# find and stop the hand-launched process, e.g.:
#   ss -tlnp | grep ':8003'
#   kill <pid>          # SIGTERM; the API flushes state on shutdown
```

## 4. Why port 8003

Port 8000 is owned on the dev host by the **honcho** container stack
(podman) and is not moved. 8003 is the canonical backend port — the
already-verified live instance serves there, the service unit pins it,
and the README/PS4 point there. On a fresh host with nothing on 8000,
**still keep 8003**: the port is part of the deployment contract, not a
host accident. Verify nothing else binds it: `ss -tlnp | grep ':8003'`.

## 5. Drift timer cadence

`cs-uk-api-drift.timer` runs the drift sweep nightly:

- `OnCalendar=*-*-* 03:10:00` — daily at 03:10 (after the API's quiet
  hours on the dev host; adjust for the new host's timezone if desired).
- `Persistent=true` — a missed run (host off at 03:10) is caught up at
  next boot.

The sweep (`cs-uk-api-drift.service`, one-shot) probes every plain-HTTP
provider's listing through the real adapters, deep-probes a rotating
subset (content → stream → HEAD) so each provider gets full coverage
every `--deep-every 6` days, and verdicts each against a
self-calibrating baseline. On a second **consecutive** failure it files
a GitHub issue via `gh` (one per provider); recovery comments and closes
it. uakino is never probed (its health is the API's browser-session
heartbeat). The machine-readable report lands in
`~/.cache/cs-uk-api/drift-report.json`; the service exits non-zero when
any provider failed — visible in `journalctl -u cs-uk-api-drift`.

## 6. Verify the deployment

```bash
systemctl is-active cs-uk-api.service        # active
systemctl status cs-uk-api-drift.timer       # active (waiting), next 03:10
ss -tlnp | grep ':8003'                      # uvicorn bound to 8003

curl -s http://127.0.0.1:8003/api/providers  # all 19 providers
# uakino reports "warming" until its browser session is ready, then "ok"

# drift sweep by hand (no issue filing):
cd /home/<user>/<repo>/backend && .venv/bin/python \
  -m cs_uk_api.scripts.drift_monitor --no-issues

# logs:
journalctl -u cs-uk-api.service -f
```

On the PS4, add a Switchfin server at `http://<host-ip>:8003` (any
username/password completes the accept-any handshake).

## 7. Day-to-day operations

```bash
sudo systemctl restart cs-uk-api.service   # re-binds 8003
sudo systemctl stop cs-uk-api.service      # graceful stop (state flushed)
journalctl -u cs-uk-api.service -f         # follow logs
rm ~/.cache/cs-uk-api/playback.json        # wipe continue-watching (single command)
rm ~/.cache/cs-uk-api/user-state.json      # wipe favorites/played
```

The README's deploy section (§Deploy) is the abbreviated version of this
runbook; this file is the durable reference for standing up a fresh host.

## 8. Release-check guard (timer fallback)

`scripts/check_releases.py` (PR #402) fails when a pushed `vX.Y.Z` tag
has no GitHub release — the v1.1.0 lesson, where the tag sat un-released
for a day and had to be backfilled. The GitHub Actions wiring is written
(branch `feat/release-check-workflow`) but not yet pushed: uploading
workflow files requires the token's `workflow` scope, which cannot be
granted non-interactively (see PR #402's body for the 2-minute enable
recipe). Until it lands, the operator-side timer below is the
enforcement; afterwards it is redundant-but-harmless belt-and-suspenders.

```bash
# install (edit User=/WorkingDirectory= in the unit first to match host):
sudo cp backend/deploy/cs-uk-api-release-check.service \
       backend/deploy/cs-uk-api-release-check.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cs-uk-api-release-check.timer

# verify by hand once (same command the unit runs):
cd /home/<user>/<repo> && python3 scripts/check_releases.py
#   release-check: 4/4 v-tags have releases   ← exit 0

systemctl list-timers cs-uk-api-release-check.timer   # next 03:40
journalctl -u cs-uk-api-release-check                 # on failure
```

Behavior: daily at **03:40** (the slot after the drift monitor's 03:10;
`Persistent=true` catches a missed run). Prereqs are the same as the
drift monitor's issue filing — `git` + `gh` on PATH with `gh`
authenticated against the repo; the script itself is stdlib-only.
Exit codes: `0` every tag has a release; `1` **drift** (each output line
names the un-released tag — fix by publishing the release, then re-run);
`2` query failure (missing binary / broken auth — operational, not
drift). A **draft-only** release still fails: an abandoned backfill is
not a release.
