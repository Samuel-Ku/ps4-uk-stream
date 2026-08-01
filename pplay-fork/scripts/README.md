# PS4 PKG build scripts

These scripts build the PS4 homebrew PKG in a reproducible Docker image
based on Ubuntu 22.04 with the OpenOrbis-PS4-Toolchain v0.5.2.

## Prerequisites

- Docker (or podman) installed on the build host.
- ~3 GB of free disk for the OpenOrbis toolchain, FFmpeg, and build outputs.
- Outbound HTTPS to fetch OpenOrbis, FFmpeg sources, and Ubuntu packages.

## Build

From the monorepo root:

```bash
./pplay-fork/scripts/build-ps4-docker.sh
```

The script will:
1. Build the `ps4-uk-build` Docker image (one-time, ~5 min).
2. Run the image, mounting the `pplay-fork/` tree.
3. Inside the image: build FFmpeg for the OpenOrbis target, then build
   pPlay with `-DPLATFORM_PS4=ON`.

The resulting artifacts:
- `pplay-fork/build/eboot.bin` — fake-signed ELF (auth_id `0x3800000000000011`).
- `pplay-fork/build/param.sfo` — title metadata.
- `pplay-fork/build/PPLA00001.pkg` — installable on a jailbroken PS4.

## Install

Transfer `PPLA00001.pkg` to the PS4 (via FTP from GoldHEN, or USB) and
install through GoldHEN's debug-menu package installer. Then:

1. In pPlay's main menu → Settings → "Адреса сервера", set the URL of
   the Linux host running the backend (e.g. `http://192.168.1.50:8000`).
2. Ensure the `data/` folder from pPlay is on the PS4 internal HDD
   (per upstream pPlay README: "PS4: install pkg and copy 'data' folder
   on ps4 internal hdd").
3. Reboot the app and open "Каталог UA".

## Manual test checklist

See [`../../docs/ps4-test-report.md`](../../docs/ps4-test-report.md) for the
checklist to fill in on the actual console.