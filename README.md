# PS4 UK Stream

A Linux-side backend that serves Ukrainian-dubbed content scraped from the
providers of cloudstream-extensions-uk, played on the PS4 through
**Switchfin** — a Jellyfin client — via the backend's Jellyfin facade
(spec #100). The backend's native `/api/*` routes and the facade serve the
same catalog; a Jellyfin client pointed at the backend finds a server
without configuration.

See `docs/superpowers/specs/2026-08-05-jellyfin-adapter.md` for the facade
design and `CONTEXT.md` for the domain model.

## Network addresses (home LAN)

- Backend host `openclaw-home`: `http://192.168.2.223:8000` (LAN IP, `enp1s0`).
- PS4 FTP (GoldHEN): `192.168.2.105:2121` (for PKG install / payload transfer).

## Quick start

0. Backend dependencies: `cd backend && pip install -r requirements.txt`.
   The uakino provider additionally needs a system Chromium binary
   (default `/usr/bin/chromium`, override with `UAKINO_CHROMIUM`): its
   Cloudflare gate only passes for in-page `fetch()` calls, so the API
   runs a headless browser session for that one provider
   (see `backend/cs_uk_api/scripts/README.md`).
1. Run the backend on a Linux host in the same LAN as the PS4:
   `cd backend && uvicorn cs_uk_api.main:app --host 0.0.0.0 --port 8000`
2. On the PS4, open Switchfin and add a server at
   `http://<host-ip>:<port>` (the backend's Jellyfin facade). Any
   username/password completes the handshake — the facade is accept-any
   and stateless (ADR-0002).

## Deploy (systemd, single LAN host)

A ready-to-edit unit ships in `backend/deploy/cs-uk-api.service`:

```bash
sudo cp backend/deploy/cs-uk-api.service /etc/systemd/system/
# edit User= / WorkingDirectory= / ExecStart= paths to this host
sudo systemctl daemon-reload
sudo systemctl enable --now cs-uk-api
journalctl -u cs-uk-api -f   # warm-up: uakino reports "warming" until its
                             # browser session is ready, then "ok"
```

The service is LAN-only by design (ADR-0003: one host, one uvicorn
process, no auth). Tunable knobs: `CS_UK_CACHE_SEARCH`,
`CS_UK_CACHE_CONTENT`, `CS_UK_CACHE_POSTER`,
`CS_UK_POSTER_DISK_TTL`, `UAKINO_CHROMIUM` — see `CONTEXT.md` §Cache
contract.

## Release gate

```bash
cd backend && . .venv/bin/activate
pytest cs_uk_api/tests -q      # 1095 tests, fixtures only (no live I/O)
ruff check cs_uk_api           # clean
mypy cs_uk_api                 # strict on the package (tests excluded in pyproject)
```

Live smoke against real upstreams (optional, needs internet):

```bash
uvicorn cs_uk_api.main:app --host 127.0.0.1 --port 8000 &
curl -s localhost:8000/api/providers        # 19 providers; uakino "warming" then "ok"
curl -s "localhost:8000/api/search?q=Дюна"  # groups + no failures
curl -s "localhost:8000/api/stream/<id>"    # live m3u8/mp4 URL, plays in mpv
```
