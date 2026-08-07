#!/usr/bin/env python3
"""
install-via-ps4debug.py — install PPLA00001 to /user/app/PPLA00001 on the PS4
via ps4debug's filesystem protocol on port 9021 (the port VUE/ps4debug
binds by default on FW 11.00).

Background
----------
The system package installer (and ItemzFlow BGFT path) reject PkgTool.Core
fPKGs with CE-34603-0 / 0x80966FFC — see ps4-test-report.md. ps4debug
v1.1.19 (GoldHEN fork) ships a kernel-level filesystem API on port 9021
that lets us mkdir / write / close directly, bypassing the system
installer entirely.

Protocol reference
------------------
Header (12 bytes, little-endian):
    magic[4] = "ccbbaaff"    (per ps4debug lib's construct struct)
    code[4]  = u32 LE
    length[4]= u32 LE

Payload follows header. After write, server replies with a 4-byte
ResponseCode (SUCCESS = 0x80000000, ERROR = 0xF0000001, …).

FS command codes (GoldHEN ps4debug fork; confirmed empirically):
    CMD_FILE_MKDIR = 0x30  payload: <u32 path_len><path bytes><u32 mode>
    CMD_FILE_OPEN  = 0x31  payload: <u32 path_len><path bytes><u32 flags><u32 mode>
    CMD_FILE_CLOSE = 0x32  payload: <u32 fd>
    CMD_FILE_WRITE = 0x33  payload: <u32 fd><u32 chunk_len><chunk bytes>
    CMD_FILE_RMDIR = 0x34  payload: <u32 path_len><path bytes>

CMD_FILE_OPEN returns its status; the file descriptor is implicit —
ps4debug's protocol reuses the connection slot index, so we always
write fd=0 for a fresh file in a fresh session and trust the kernel
to track the vnode.

Verification
------------
After every WRITE, we open a side-channel FTP connection and `SIZE` the
remote file. If sizes match, the write really happened. This catches
the case we hit before, where send_command() returned a coroutine that
was never awaited — the script "printed success" but actually wrote
nothing.

Usage
-----
    python3 install-via-ps4debug.py \\
        --host 192.168.2.105 --port 9021 \\
        --title-id PPLA00001 \\
        --eboot   pplay-fork/build/eboot.bin \\
        --param-sfo pplay-fork/build/data_romfs/sce_sys/param.sfo \\
        --icon    pplay-fork/build/data_romfs/sce_sys/icon0.png
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import struct
import sys
from contextlib import asynccontextmanager
from ftplib import FTP, error_perm
from typing import AsyncIterator

from ps4debug import PS4Debug, PS4DebugException, core

# --- ps4debug FS protocol constants -----------------------------------------
CMD_FILE_MKDIR = 0x30
CMD_FILE_OPEN = 0x31
CMD_FILE_CLOSE = 0x32
CMD_FILE_WRITE = 0x33
CMD_FILE_RMDIR = 0x34

O_WRONLY = 0x01
O_CREAT = 0x02
O_TRUNC = 0x04

WRITE_CHUNK = 64 * 1024  # 64 KiB — ps4debug's preferred chunk size

log = logging.getLogger("install-ps4debug")


def _p32(n: int) -> bytes:
    return struct.pack("<I", n & 0xFFFFFFFF)


class InstallError(RuntimeError):
    """Anything that prevents a clean install: bad status, size mismatch."""


async def _send(ps4: PS4Debug, code: int, payload: bytes) -> int:
    """Send one raw command, await the server status, return it as int."""
    try:
        status = await ps4.send_command(code, payload, status=True)
    except Exception as e:
        raise InstallError(f"send_command(0x{code:02x}) raised: {e}") from e
    if status is None:
        # status=False would give None; we always pass status=True so this
        # should never happen, but guard anyway.
        raise InstallError(f"send_command(0x{code:02x}) returned None status")
    code_int = int(status)
    if code_int != int(core.ResponseCode.SUCCESS):
        raise InstallError(
            f"send_command(0x{code:02x}) status=0x{code_int:08x} "
            f"(expected SUCCESS=0x{int(core.ResponseCode.SUCCESS):08x})"
        )
    return code_int


async def _mkdir(ps4: PS4Debug, path: str, mode: int = 0o755) -> None:
    payload = _p32(len(path)) + path.encode("utf-8") + _p32(mode)
    await _send(ps4, CMD_FILE_MKDIR, payload)
    log.info("mkdir %s", path)


async def _rmdir(ps4: PS4Debug, path: str) -> None:
    payload = _p32(len(path)) + path.encode("utf-8")
    # RMDIR may fail with ERROR if the dir is missing — that's fine for
    # the idempotent cleanup pass.
    try:
        await _send(ps4, CMD_FILE_RMDIR, payload)
        log.info("rmdir %s", path)
    except InstallError as e:
        log.info("rmdir %s skipped: %s", path, e)


async def _write_file(
    ps4: PS4Debug,
    remote: str,
    data: bytes,
    *,
    ftp: FTP | None = None,
) -> None:
    """Write `data` to `remote` via ps4debug, then SIZE-check via FTP.

    The FTP SIZE check is what makes this honest: if the send_command
    coroutine is mishandled, the size mismatch raises InstallError
    instead of silently lying in stdout.
    """
    payload = (
        _p32(len(remote))
        + remote.encode("utf-8")
        + _p32(O_WRONLY | O_CREAT | O_TRUNC)
        + _p32(0o644)
    )
    await _send(ps4, CMD_FILE_OPEN, payload)
    log.info("open  %s", remote)

    fd = 0  # ps4debug's protocol reuses the slot index for fresh OPENs
    offset = 0
    while offset < len(data):
        chunk = data[offset : offset + WRITE_CHUNK]
        write_payload = _p32(fd) + _p32(len(chunk)) + chunk
        await _send(ps4, CMD_FILE_WRITE, write_payload)
        offset += len(chunk)
        if offset % (WRITE_CHUNK * 8) == 0 or offset == len(data):
            log.info(
                "  write %7d B   total %d/%d", len(chunk), offset, len(data)
            )

    await _send(ps4, CMD_FILE_CLOSE, _p32(fd))
    log.info("close %s  (%d B)", remote, len(data))

    # Honest verification — read the size back via FTP and compare.
    if ftp is not None:
        try:
            size_remote = ftp.size(remote)
        except error_perm as e:
            raise InstallError(f"FTP SIZE {remote} failed: {e}") from e
        if size_remote != len(data):
            raise InstallError(
                f"size mismatch for {remote}: wrote {len(data)} B, "
                f"FTP reports {size_remote} B"
            )
        log.info("verify OK  %s  %d B", remote, size_remote)


@asynccontextmanager
async def _ps4(host: str, port: int) -> AsyncIterator[PS4Debug]:
    """Context manager that opens one PS4Debug session and closes it on exit."""
    ps4 = PS4Debug(host, port)
    log.info("connected to ps4debug %s:%d", host, port)
    try:
        yield ps4
    finally:
        # SocketPool has no explicit close; the GC destructor closes writers.
        # Eagerly drop our reference so the sockets close now rather than
        # waiting for Python's GC.
        del ps4


@asynccontextmanager
async def _ftp(host: str) -> AsyncIterator[FTP]:
    """Context manager around a GoldHEN FTP connection."""
    ftp = FTP()
    ftp.connect(host, 2121, timeout=10)
    ftp.login("anonymous", "")
    log.info("FTP banner: %s", ftp.getwelcome())
    try:
        yield ftp
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


async def install(
    host: str,
    port: int,
    title_id: str,
    eboot_path: str,
    param_sfo_path: str,
    icon_path: str,
) -> None:
    app_root = f"/user/app/{title_id}"
    sys_root = f"{app_root}/sce_sys"

    with open(eboot_path, "rb") as f:
        eboot = f.read()
    with open(param_sfo_path, "rb") as f:
        param_sfo = f.read()
    with open(icon_path, "rb") as f:
        icon = f.read()
    log.info(
        "payloads: eboot=%d B, param.sfo=%d B, icon0.png=%d B",
        len(eboot), len(param_sfo), len(icon),
    )

    async with _ps4(host, port) as ps4, _ftp(host) as ftp:
        # Idempotent cleanup pass — best-effort, ignore failures so a
        # half-installed tree from a previous attempt doesn't shadow new files.
        for d in (sys_root, app_root):
            await _rmdir(ps4, d)

        await _mkdir(ps4, app_root)
        await _mkdir(ps4, sys_root)

        await _write_file(ps4, f"{app_root}/eboot.bin", eboot, ftp=ftp)
        await _write_file(ps4, f"{sys_root}/param.sfo", param_sfo, ftp=ftp)
        await _write_file(ps4, f"{sys_root}/icon0.png", icon, ftp=ftp)

    log.info("Install complete.")
    log.info("  Title ID:  %s", title_id)
    log.info("  App root:  %s", app_root)
    log.info("  Restart SceShellUI (or reboot) to register the title.")
    log.info("  Or trigger GoldHEN BinLoader to launch directly.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="192.168.2.105")
    p.add_argument("--port", type=int, default=9021,
                   help="ps4debug port (VUE binds 9021; standalone ps4debug defaults to 7447)")
    p.add_argument("--title-id", default="PPLA00001")
    p.add_argument("--eboot", required=True)
    p.add_argument("--param-sfo", required=True)
    p.add_argument("--icon", required=True)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        asyncio.run(install(
            host=args.host,
            port=args.port,
            title_id=args.title_id,
            eboot_path=args.eboot,
            param_sfo_path=args.param_sfo,
            icon_path=args.icon,
        ))
    except (InstallError, PS4DebugException) as e:
        log.error("FAILED: %s", e)
        return 1
    except (ConnectionRefusedError, OSError) as e:
        log.error("connection failed: %s — is ps4debug listening on %s:%d?",
                  e, args.host, args.port)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
