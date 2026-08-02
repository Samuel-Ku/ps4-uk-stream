# PS4 UK Stream

Fork of pPlay v3.8 with a new "Каталог UA" screen and a Linux-side backend
that serves Ukrainian-dubbed content scraped from the providers of
cloudstream-extensions-uk.

See `docs/superpowers/specs/2026-08-01-ps4-uk-stream-design.md` for design,
and `docs/superpowers/plans/2026-08-01-ps4-uk-stream-impl.md` for the
implementation plan.

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
2. Build the PS4 PKG (see `pplay-fork/`).
3. Install the PKG via GoldHEN on a PS4 running firmware 11.00.
4. In pPlay settings, set "Адреса сервера" to `http://<host-ip>:8000`.
