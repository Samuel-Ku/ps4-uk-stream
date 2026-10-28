# Remote hosts

Install and operations runbook: [`backend/deploy/DEPLOY.md`](../../backend/deploy/DEPLOY.md).

## openclaw-home

`openclaw-home` is the LAN deployment host, **not** an authoring workspace.

- SSH: `ssh openclaw-home`
- User: `semen`
- Checkout: `/home/semen/ps4-uk-stream` (backend: `/home/semen/ps4-uk-stream/backend`)
- Backend venv: `backend/.venv` (runtime dependencies only)
- Services: user units under `~/.config/systemd/user/` — `cs-uk-api.service`
  and `cs-uk-api-drift.timer`; manage them with `systemctl --user`.
- Runtime state: `~/.cache/cs-uk-api/` (posters, playback, user state, drift
  report/state).
- LAN address: `192.168.2.223` — **static** since 2026-10-28
  (`/etc/netplan/50-cloud-init.yaml`, `dhcp4: false`; the previous DHCP
  config is kept at `/root/netplan-50-cloud-init.yaml.bak-20261028`). It was
  DHCP before and the lease moved (`.166` once, `.223` now), which silently
  broke the PS4 client's saved server URL — check this file before blaming
  the console when the facade becomes unreachable.
- Router: Cudy with OpenWrt/LuCI at `http://192.168.2.1:8080` (SSH: dropbear
  on `:22`), DHCP server `192.168.2.1`. The pool still contains `.223`; the
  host answers ARP, so dnsmasq skips it, but a LuCI static lease/exclusion is
  the durable fix if the pool ever fills up.

The host path differs from the workstation clone. Do not assume
`~/Projects/ps4-uk-stream` exists on the host.

### Git contract

Author, test and commit in the workstation clone. The host only receives
fast-forwards:

```bash
ssh openclaw-home
cd ~/ps4-uk-stream
git fetch origin
git merge --ff-only origin/master
```

- Do not commit from the host. If a change exists only there, copy it to the
  workstation clone, run the release gate, push, then fast-forward the host.
- The host checkout may have no git identity configured. If a commit there is
  unavoidable, pass one explicitly:
  `git -c user.name='...' -c user.email='...' commit ...`
- Keep tracked files clean. `backend/deploy/config/`,
  `backend/deploy/torrent-data/` and `*.bak-*` are runtime/backup artifacts,
  not commit candidates.
- Leave the runtime directories in place; do not clean them without the
  operator.

### Verification

- The host venv is runtime-only; `pytest` is not installed. Run the release
  gate in the workstation clone or CI: `pytest cs_uk_api/tests -q`,
  `ruff check cs_uk_api`, `mypy cs_uk_api`.
- Host smoke: `backend/deploy/smoke.sh` (exit `0` = healthy).
- Drift sweep without tracker writes:
  `cd backend && .venv/bin/python -m cs_uk_api.scripts.drift_monitor --no-issues`.
- Service/timer status: `systemctl --user status cs-uk-api.service` and
  `systemctl --user list-timers cs-uk-api-drift.timer`.
- Drift issue filing needs an authenticated `gh` on the host; see
  `DEPLOY.md` §0 and §5.

### Tooling

- `ffmpeg` is installed from apt (`/usr/bin/ffmpeg`).
- `fpcalc` (Chromaprint tools) is installed **user-locally**, without root:
  the Ubuntu 24.04 `libchromaprint-tools` 1.5.1-5 `.deb` is extracted under
  `~/.local/opt/chromaprint/` (binary `usr/bin/fpcalc`, the `.deb` kept
  beside it) and symlinked as `~/.local/bin/fpcalc`. The host already had
  `libchromaprint1` 1.5.1-5 and the FFmpeg 6 shared libraries, so the
  extracted binary runs against the system libraries (`ldd` reports no
  missing objects).
- The service finds it through the drop-in
  `~/.config/systemd/user/cs-uk-api.service.d/fpcalc-path.conf`, which
  prepends `~/.local/bin` to `PATH`: systemd's default service `PATH`
  excludes that directory and the teleport aligner resolves `fpcalc` with
  `shutil.which()`. After changing the drop-in run
  `systemctl --user daemon-reload && systemctl --user restart
  cs-uk-api.service`.
- A host with root can instead `apt install ffmpeg libchromaprint-tools`
  (README §Deploy); this host uses the root-free variant.
- Verified 2026-10-27: `fpcalc version 1.5.1` (`Lavc60.31.102`); a cold
  `POST /api/teleport/align` for `serialno:28-gra-prestoliv:s1e1` →
  `uaserialspro:274-gra-prestoliv-s2011:s1e1` at 600 s returned
  `200 matched` (offset `+0.019 s`, confidence `0.9881`, 15.1 s).

## PS4 console

The living-room client and its GoldHEN access (FTP `:2121`, KLog `:3232`),
the filesystem map, custom-PKG install/rollback and the FTP quirks that bite:
[`ps4-console.md`](ps4-console.md).
