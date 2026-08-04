#!/usr/bin/env python3
"""audit_upstream_pkg.py — list the file table of an OpenOrbis / Sony .PKG file.

Bug #18 (postmortem-ps4-install-attempts.md): our build's PPLA00001.pkg is
7 MB while upstream pPlay 3.8 is 33 MB. The diff turned out to be the
eboot.bin (SELF) content — the file table itself is identical between
our build and upstream (5 PS4 system files: param.sfo, icon0.png, and
3 PlayGo auxiliary files). This script makes that equivalence testable:

  python3 scripts/audit_upstream_pkg.py /path/to/IV0001-PPLA00001_*.pkg

Prints one line per file in the container's file table, in the layout:

    <name>  <data_offset>  <data_size>  <sha256_prefix>

The output is intentionally diff-friendly so we can `diff -u` the audit
of our build vs upstream and see if anything has regressed.

Format reference
----------------
OpenOrbis .CNT (7F 43 4E 54) and Sony .PKG (7F 50 4B 47) share the same
layout. The body of the file is encrypted, but the file table is plain.
The interesting offsets are:

  0x0000  magic (4) + revision (4) + pkg_type (2) + header_size (2)
          + item_count (4) + total_size (8) + data_offset (8)
          + data_count (8) + content_id (36) + ...
  0x0x2A80  item_count entries, each 32 bytes, big-endian:
              id (4) | name_table_offset (4) | flags1 (4) | flags2 (4)
              | data_offset (4) | data_size (4) | pad (8)
  0x2A80 + 32*item_count  names table, null-terminated ASCII strings
          packed back-to-back (no length prefix).

See docs/postmortem-ps4-install-attempts.md Bug #18 for the full
investigation that derived this layout empirically.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

# --- PKG format constants -------------------------------------------------

MAGIC_CNT = b"\x7fCNT"  # OpenOrbis PkgTool.Core output
MAGIC_PKG = b"\x7fPKG"  # Sony PKG (same body layout)
HEADER_SIZE = 0x200

# The file table lives at a fixed offset in every .CNT/.PKG we have seen
# (Bug #18): header (0x200 bytes) + 4 SHA256 hashes of body chunks
# (4 * 32 bytes = 0x80) + another 0x80 of header parity + 0x800 of
# pre-table padding. PkgTool.Core / orbis-pub-cmd both emit the table
# here. See docstring.
FILE_TABLE_OFFSET = 0x2A80
FILE_ENTRY_SIZE = 32
NAMES_TABLE_OFFSET = 0x2E00  # observed empirically; depends on file_table size

BIG_ENDIAN = "big"


# --- data model -----------------------------------------------------------


@dataclass(frozen=True)
class FileEntry:
    """One row of the PKG file table. Immutable per ECC coding-style."""

    name: str
    data_offset: int
    data_size: int
    index: int


# --- helpers --------------------------------------------------------------


def sha256_prefix(data: bytes, n: int = 4) -> str:
    """First `n` bytes of SHA256(data), hex-encoded.

    4 bytes = 8 hex chars. That's enough to distinguish files in a
    diff-friendly listing without flooding the output.
    """
    digest = hashlib.sha256(data).digest()[:n]
    return digest.hex()


def _read_exact(stream: BinaryIO, n: int, ctx: str) -> bytes:
    chunk = stream.read(n)
    if len(chunk) != n:
        raise ValueError(f"short read in {ctx}: expected {n} got {len(chunk)}")
    return chunk


def _check_magic(path: Path, f: BinaryIO) -> tuple[int, int]:
    """Verify the magic and return (revision, item_count)."""
    head = _read_exact(f, HEADER_SIZE, "header")
    if not (head.startswith(MAGIC_CNT) or head.startswith(MAGIC_PKG)):
        raise ValueError(
            f"{path}: not a .PKG/.CNT (magic {head[:4]!r}, "
            f"expected {MAGIC_CNT!r} or {MAGIC_PKG!r})"
        )
    revision = int.from_bytes(head[4:8], BIG_ENDIAN)
    item_count = int.from_bytes(head[12:16], BIG_ENDIAN)
    return revision, item_count


def _read_file_table(f: BinaryIO, item_count: int) -> list[FileEntry]:
    """Read `item_count` 32-byte entries starting at FILE_TABLE_OFFSET."""
    entries: list[FileEntry] = []
    for i in range(item_count):
        # Seek to the absolute position of this entry — _read_name() before
        # this call may have moved the file cursor to the end of the name.
        f.seek(FILE_TABLE_OFFSET + i * FILE_ENTRY_SIZE)
        raw = _read_exact(f, FILE_ENTRY_SIZE, f"file entry {i}")
        # Each entry: id(I) name_table_offset(I) flags1(I) flags2(I)
        # data_offset(I) data_size(I) pad(8)
        _id = int.from_bytes(raw[0:4], BIG_ENDIAN)
        name_off = int.from_bytes(raw[4:8], BIG_ENDIAN)
        _flags1 = int.from_bytes(raw[8:12], BIG_ENDIAN)
        _flags2 = int.from_bytes(raw[12:16], BIG_ENDIAN)
        data_offset = int.from_bytes(raw[16:20], BIG_ENDIAN)
        data_size = int.from_bytes(raw[20:24], BIG_ENDIAN)
        # name_off is the offset INTO the names table (relative to
        # NAMES_TABLE_OFFSET). name_off == 0 means the name is at the start
        # of the names table — NOT an empty name. The only signal for an
        # intentionally empty name is id == 0 (directory entries use id=0
        # and have no file payload).
        name = _read_name(f, name_off) if _id != 0 else ""
        entries.append(
            FileEntry(
                name=name,
                data_offset=data_offset,
                data_size=data_size,
                index=i,
            )
        )
    return entries


def _read_name(f: BinaryIO, name_off: int) -> str:
    """Read a null-terminated ASCII name at NAMES_TABLE_OFFSET + name_off."""
    f.seek(NAMES_TABLE_OFFSET + name_off)
    buf = bytearray()
    while True:
        b = f.read(1)
        if not b or b == b"\x00":
            break
        buf += b
    return buf.decode("ascii", errors="replace")


def _sha_for_entry(f: BinaryIO, entry: FileEntry, chunk: int = 65536) -> str:
    """Read the file's bytes-bytes and return SHA256 prefix.

    The PKG body is encrypted (AES-CTR), but for the audit we don't need
    real ciphertext SHA — we want a fingerprint that's stable across
    PKG files at the byte level. Reading the raw bytes-as-stored gives us
    that: same input file → same SHA; different input → different SHA.
    """
    f.seek(entry.data_offset)
    h = hashlib.sha256()
    remaining = entry.data_size
    while remaining > 0:
        block = f.read(min(chunk, remaining))
        if not block:
            break
        h.update(block)
        remaining -= len(block)
    return h.hexdigest()[:8]


def parse_pkg(path: Path) -> list[FileEntry]:
    """Parse the file table of a .PKG/.CNT and return the entries.

    Reads the header + file table only. Body is only read on demand
    for SHA256 fingerprinting.
    """
    with path.open("rb") as f:
        _revision, item_count = _check_magic(path, f)
        entries = _read_file_table(f, item_count)
        # Compute SHA256 prefix for each entry by reading the on-disk bytes.
        # Reading the file again in a second pass keeps the seek state simple.
        for entry in entries:
            entry_sha_prefix = _sha_for_entry(f, entry)
            # Attach via object.__setattr__ because FileEntry is frozen;
            # we'd rather keep the API simple by re-reading.
            object.__setattr__(entry, "_sha_prefix", entry_sha_prefix)
    return entries


# --- CLI ------------------------------------------------------------------


def _format_line(entry: FileEntry) -> str:
    sha = getattr(entry, "_sha_prefix", "")
    return f"{entry.name:<28}  {entry.data_offset:>10}  {entry.data_size:>10}  {sha}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "pkg",
        type=Path,
        nargs="+",
        help="One or more .PKG/.CNT files to audit.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Include header fields (magic, item_count, file size).",
    )
    args = parser.parse_args(argv)

    failed = 0
    for path in args.pkg:
        if not path.exists():
            print(f"ERROR: {path} does not exist", file=sys.stderr)
            failed += 1
            continue
        try:
            entries = parse_pkg(path)
        except (ValueError, OSError) as e:
            print(f"ERROR: {path}: {e}", file=sys.stderr)
            failed += 1
            continue

        if args.verbose:
            print(f"# {path}")
            print(f"# size: {path.stat().st_size} B")
            print(f"# entries: {len(entries)}")
            print(f"# {'name':<28}  {'offset':>10}  {'size':>10}  {'sha256[:4]':<8}")
        for entry in entries:
            print(_format_line(entry))
        if args.verbose and len(args.pkg) > 1:
            print()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
