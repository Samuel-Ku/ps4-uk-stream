# PS4 console

The console is the living-room client; `openclaw-home` serves the facade it
talks to. Everything below was verified against the actual device (GoldHEN)
on 2026-10-28 — read it before rediscovering any of it over FTP.

## Access

- LAN address: `192.168.2.105` (DHCP; if it moves, the client cannot reach
  the facade at all — check the router / ask the owner).
- GoldHEN FTP: `ftp://192.168.2.105:2121`, `anonymous`/`anonymous`, root of
  the console filesystem; reads and writes both work.
- GoldHEN KLog: TCP `192.168.2.105:3232` — the raw console log stream
  (kernel + app). `timeout 8 socat -u TCP:192.168.2.105:3232 - | grep -a
  SFIN` is enough; it only emits lines while something runs.
- BinLoader: TCP `9090` (payload loader; unused by this project so far).
- **No remote input.** No network channel presses a button: installing from
  the store, launching an app and accepting dialogs are done by the person
  in the room. Design verification steps around that and ask for them.

## Filesystem map

| path | what it is |
| --- | --- |
| `/user/app/SFIN00000/` | installed Switchfin (homebrew 0.9.4): `app.pkg`, `eboot.bin` |
| `/data/Switchfin/config.json` | the client's saved servers/users/token — the file that made login "time out" while it still pointed at the workstation's loopback-only dev facade |
| `/data/pkg/` | our PKG copies; `SFIN00000-stock-0.9.4.pkg` is the rollback image |
| `/data/GoldHEN/plugins.ini` | auto-injected GoldHEN plugin list; the removed `.prx` files are parked in `plugins_disabled_*/` |
| `/user/app/NPXS39041/` | the HB Store app: `storedata/settings.ini`, `storedata/store.db` |
| `/user/app/NPXS39041/downloads/` | staged homebrew PKGs; the store enumerates `{title id}.pkg` here |

`/data/Switchfin/` survives a reinstall of the app package.

## Installing a custom PKG

1. Upload the built `…-SFIN00000…-0.9.4.pkg` over FTP to
   `/user/app/NPXS39041/downloads/SFIN00000.pkg` — the file name must be the
   title id, the store keys on it.
2. Verify the upload: `SIZE` of the staged file equals the local byte count
   (58 523 648 for the custom 0.9.4 build). The row's `md5` in `store.db` is
   `NULL`, so the store does not hash-check a custom build.
3. On the console: HB Store → Switchfin → **Install** (the entry shows
   *Ready to install* while the staged file is there). Do **not** press
   *Reinstall Latest* / *Download and Install* — those re-download the stock
   0.9.4 from the CDN and overwrite the staged file.
4. Proof from the workstation: `SIZE /user/app/SFIN00000/app.pkg` equals the
   staged PKG size, and the backend journal shows `192.168.2.105` requests
   once the app is launched.
5. Rollback: stage `/data/pkg/SFIN00000-stock-0.9.4.pkg` the same way, or use
   the store's *Reinstall Latest* (it re-downloads stock).

## FTP quirks (GoldHEN's ftpd)

- `SIZE` is truthful; `LIST`/MLSD sizes are not — always use `SIZE`.
- `RETR` can truncate mid-file (observed: an 18 KB payload arrived as 400 B).
  Compare the received byte count with `SIZE` and retry; a plausible-looking
  small file can silently be a truncated one.
- Uploads are not atomic: a killed `STOR` leaves a partial file, so verify
  sizes after `put`.
- `curl ftp://…` works for both directions; add `--ftp-pasv` if a NAT sits in
  between.

## Client facts

- The client is `dragonflylee/switchfin`; the custom build lives on branch
  `client-build/ps4-v1` (`client/ps4/` — patches plus the Actions workflow),
  install and rollback in `client/ps4/README.md`.
- The PS4 system IME has no Ukrainian layout (OpenOrbis exposes Russian
  only), so the in-app on-screen keyboard is the only way to type Ukrainian.
- The client has no trailer support at all — no trailer JSON, no trailer UI.
- Default HTTP timeout is 3 s. A fresh `config.json` carries no
  `request_timeout`, while the facade's cold home load (19 parallel "latest"
  shelves) takes 6–8 s: the stock client answers with "Timeout was reached"
  and empty home rows.

## Backend coupling

- If `openclaw-home`'s LAN address changes, the saved URL in
  `/data/Switchfin/config.json` must be updated too — the host is static
  `.223` since 2026-10-28 (see [`remote-hosts.md`](remote-hosts.md)).
- A poster retry storm (one image requested hundreds of times) means a poster
  host is missing from the facade's allowlist, not a console fault.
