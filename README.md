# PS4 UK Stream

Fork of pPlay v3.8 with a new "Каталог UA" screen and a Linux-side backend
that serves Ukrainian-dubbed content scraped from the providers of
cloudstream-extensions-uk.

See `docs/superpowers/specs/2026-08-01-ps4-uk-stream-design.md` for design,
and `docs/superpowers/plans/2026-08-01-ps4-uk-stream-impl.md` for the
implementation plan.

## Quick start

1. Run the backend on a Linux host in the same LAN as the PS4:
   `cd backend && uvicorn cs_uk_api.main:app --host 0.0.0.0 --port 8000`
2. Build the PS4 PKG (see `pplay-fork/`).
3. Install the PKG via GoldHEN on a PS4 running firmware 11.00.
4. In pPlay settings, set "Адреса сервера" to `http://<host-ip>:8000`.
