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
1. Build the `ps4-uk-build` Docker image (one-time, ~10 min — includes
   a cross-build of OpenSSL 3.0 for the PS4 sysroot, #35).
2. Run the image, mounting the `pplay-fork/` tree.
3. Inside the image (`run-in-ps4-container.sh`): cross-build FFmpeg
   with `--enable-openssl` (https/tls protocols), then build pPlay
   with `-DPLATFORM_PS4=ON`, then fake-sign with create-fself
   (`paid=0x3800000000000011`, `ptype=npdrm_exec`) and pack the
   installable pkg via PkgTool.Core.
4. Validate the artifacts (PKG magic `7f 43 4e 54`, SELF magic
   `4f 15 3d 1d`, readelf on the unsigned ELF) and print the FFmpeg
   protocol-config evidence.

The resulting artifacts:
- `pplay-fork/build/eboot.bin` — fake-signed SELF (auth_id `0x3800000000000011`).
- `pplay-fork/build/pplay.elf` — the unsigned ELF (readelf inspects this one).
- `pplay-fork/build/param.sfo` — title metadata.
- `pplay-fork/build/PPLA00001.pkg` — installable on a jailbroken PS4
  (normalised name for `IV0000-PPLA00001_00-PPLAY00000000000.pkg`).

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