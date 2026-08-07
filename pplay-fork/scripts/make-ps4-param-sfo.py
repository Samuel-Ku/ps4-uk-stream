#!/usr/bin/env python3
"""Generate a param.sfo in the compact layout PkgTool.Core (LibOrbisPkg)
reads and re-emits inside the pkg: key entries are 16 bytes as
koff(u16) + fmt(u16) + sz(u32) + max(u32) + doff(u32), key names live
at header.kt + koff, values at header.dt + doff.

Field set follows the proven-working upstream pPlay 3.8 SFO
(APP_TYPE=1, SYSTEM_VER as u32, CATEGORY="gde", FORMAT="obs", strings
with fmt 0x0204, ints with fmt 0x0404) while keeping our own
CONTENT_ID / TITLE / VERSION. PUBTOOLINFO + PUBTOOLVER are added by
PkgTool.Core at pkg build time, so they are intentionally absent here.

Usage: make-ps4-param-sfo.py [output.sfo]
"""
import struct
import sys

MAGIC = b"\x00PSF"
VER = 0x00000101
FMT_INT = 0x0404
FMT_STR = 0x0204

CONTENT_ID = "IV0000-PPLA00001_00-PPLAY00000000000"

FIELDS = [
    ("APP_TYPE", FMT_INT, 1),
    ("APP_VER", FMT_STR, "01.00"),
    ("ATTRIBUTE", FMT_INT, 0x10000),
    ("CATEGORY", FMT_STR, "gde"),
    ("CONTENT_ID", FMT_STR, CONTENT_ID),
    ("DOWNLOAD_DATA_SIZE", FMT_INT, 0),
    ("FORMAT", FMT_STR, "obs"),
    ("SYSTEM_VER", FMT_INT, 0x3FC),
    ("TITLE", FMT_STR, "pPlay UK Stream"),
    ("TITLE_ID", FMT_STR, "PPLA00001"),
    ("VERSION", FMT_STR, "01.00"),
]


def build():
    n = len(FIELDS)
    kt = 20 + n * 16
    names = b"".join(k.encode("ascii") + b"\x00" for k, _, _ in FIELDS)
    pad = (4 - len(names) % 4) % 4
    dt = kt + len(names) + pad

    entries = bytearray()
    blobs = []
    for _, fmt, val in FIELDS:
        if fmt == FMT_INT:
            raw = struct.pack("<I", val)
        else:
            raw = val.encode("utf-8") + b"\x00"
        max_len = (len(raw) + 3) & ~3
        entries += struct.pack("<HHI", 0, fmt, len(raw))
        entries += struct.pack("<II", max_len, 0)
        blobs.append(raw + b"\x00" * (max_len - len(raw)))

    name_off = 0
    data_off = 0
    for i, (key, _, _) in enumerate(FIELDS):
        struct.pack_into("<H", entries, i * 16, name_off)
        struct.pack_into("<I", entries, i * 16 + 12, data_off)
        name_off += len(key) + 1
        data_off += len(blobs[i])

    header = MAGIC + struct.pack("<IIII", VER, kt, dt, n)
    return header + bytes(entries) + names + b"\x00" * pad + b"".join(blobs)


def verify(sfo):
    _, ver, kt, dt, n = struct.unpack("<IIIII", sfo[:20])
    assert sfo[:4] == MAGIC and ver == VER, "header"
    print(f"OK: {len(sfo)}B n={n} kt=0x{kt:x} dt=0x{dt:x}")
    for i in range(n):
        koff, fmt, sz, mx, doff = struct.unpack("<HHIII", sfo[20 + i * 16:36 + i * 16])
        name = sfo[kt + koff:].split(b"\x00", 1)[0].decode("ascii")
        raw = sfo[dt + doff:dt + doff + sz]
        val = struct.unpack("<I", raw)[0] if fmt == FMT_INT else raw.decode("utf-8")
        print(f"  {name:24s} fmt={fmt:#06x} sz={sz:3d} = {val}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "param.sfo"
    sfo = build()
    with open(out, "wb") as f:
        f.write(sfo)
    verify(sfo)
    print(f"written: {out}")
