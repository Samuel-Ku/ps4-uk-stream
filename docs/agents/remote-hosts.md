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
