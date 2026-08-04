"""Tests for scripts/audit_upstream_pkg.py.

Bug #18 (postmortem-ps4-install-attempts.md): our build's PKG is 7 MB while
upstream pPlay 3.8 is 33 MB. The audit script must prove the ON-DISK file
table is equivalent (same 5 PS4 system files), so the diff is in the SELF
runtime (subfont.ttf, ffmpeg, libc) — captured by issue #94.

We mock the upstream PKG by writing a fixture to a tmp file with the
exact byte layout observed in /tmp/pplay-upstream/pplay/IV0001-PPLA00001_00-PPLA000013080000.pkg.
"""
from __future__ import annotations

import dataclasses
import struct
from pathlib import Path

import pytest

from scripts.audit_upstream_pkg import (
    HEADER_SIZE,
    MAGIC_CNT,
    MAGIC_PKG,
    NAMES_TABLE_OFFSET,
    FileEntry,
    parse_pkg,
    sha256_prefix,
    sha_prefixes_for_entries,
)


# --- magic constants -----------------------------------------------------

def test_magic_constants_are_correct():
    """Per OpenOrbis .CNT format (7F 43 4E 54) and Sony .PKG (7F 50 4B 47)."""
    assert MAGIC_CNT == b"\x7fCNT"
    assert MAGIC_PKG == b"\x7fPKG"
    assert HEADER_SIZE == 0x200
    # Locked in: every .CNT/.PKG we have observed puts the names table at 0x2E00.
    # If this changes, the postmortem investigation needs to be redone.
    assert NAMES_TABLE_OFFSET == 0x2E00


# --- sha256_prefix -------------------------------------------------------

def test_sha256_prefix_returns_8_hex_chars():
    digest = sha256_prefix(b"hello")
    assert len(digest) == 8
    # sha256("hello")[:4] in hex = 2cf24dba
    assert digest == "2cf24dba"


def test_sha256_prefix_is_content_addressed():
    """Same bytes → same prefix; different bytes → different prefix."""
    assert sha256_prefix(b"abc") == sha256_prefix(b"abc")
    assert sha256_prefix(b"abc") != sha256_prefix(b"abd")


# --- FileEntry -----------------------------------------------------------

def test_file_entry_frozen_dataclass():
    """FileEntry must be immutable — see ecc/common/coding-style.md."""
    e = FileEntry(name="eboot.bin", data_offset=0, data_size=1024, index=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.name = "param.sfo"  # type: ignore[misc]


# --- parse_pkg -----------------------------------------------------------

def _build_fixture(tmp_path: Path, *, entries: list[tuple[str, int, int]]) -> Path:
    """Build a minimal .CNT fixture with the exact OpenOrbis layout.

    entries: [(name, data_offset, data_size), ...] — names offsets are
    assigned automatically in the order given, packed back-to-back and
    4-byte aligned.

    The fixture layout matches the real PKG layout observed empirically:
        0x0000–0x2A7F   header + padding (zeros, except header fields)
        0x2A80–         file table (32 bytes * item_count)
        0x2E00–         names table (NULL-terminated ASCII, 4-byte aligned)
        ... body data (zeros, sized to fit the largest data_offset+size)

    The names table offset is taken from the implementation constant, not
    hardcoded here, so this fixture stays faithful if the offset is ever
    re-derived.
    """
    total_data = sum(size for _, _, size in entries)
    max_end = max((off + size for _, off, size in entries), default=0)

    # Build the names blob first so we know the offsets.
    names_blob = bytearray()
    name_offsets: list[int] = []
    for name, _, _ in entries:
        name_offsets.append(len(names_blob))
        names_blob += name.encode("ascii") + b"\x00"
    while len(names_blob) % 4:
        names_blob += b"\x00"

    # Pre-table region (0x0000 .. 0x2A7F): zeros, except for the header
    # fields parse_pkg actually reads.
    pre_table = bytearray(0x2A80)
    pre_table[0:4] = MAGIC_CNT
    struct.pack_into(">I", pre_table, 4, 1)             # revision
    struct.pack_into(">H", pre_table, 8, 0)             # pkg_type
    struct.pack_into(">I", pre_table, 12, len(entries)) # item_count
    struct.pack_into(">Q", pre_table, 32, total_data)   # data_count

    # File table at 0x2A80.
    file_table = bytearray()
    for i, (_name, data_offset, data_size) in enumerate(entries):
        file_table += struct.pack(
            ">IIIIII",
            1,                  # id (file)
            name_offsets[i],    # name_offset (relative to NAMES_TABLE_OFFSET)
            0,                  # flags1
            0,                  # flags2
            data_offset,        # data_offset
            data_size,          # data_size
        )
        file_table += b"\x00" * 8  # pad

    # Pad between file_table_end and NAMES_TABLE_OFFSET with zeros.
    file_table_end = 0x2A80 + len(file_table)
    gap = bytearray(NAMES_TABLE_OFFSET - file_table_end)

    # Body data — zeros sized to fit the largest entry.
    body = bytearray(max_end)

    fixture = bytes(pre_table) + bytes(file_table) + bytes(gap) + bytes(names_blob) + bytes(body)
    path = tmp_path / "test.pkg"
    path.write_bytes(fixture)
    return path


def test_parse_pkg_rejects_bad_magic(tmp_path):
    bad = tmp_path / "bad.pkg"
    bad.write_bytes(b"NOTAPKG" + b"\x00" * (HEADER_SIZE - 7))
    with pytest.raises(ValueError, match="magic"):
        parse_pkg(bad)


def test_parse_pkg_reads_5_entries(tmp_path):
    entries = [
        ("icon0.png", 0, 1024),
        ("param.sfo", 1024, 512),
        ("playgo-chunk.dat", 1536, 256),
        ("playgo-chunk.sha", 1792, 128),
        ("playgo-manifest.xml", 1920, 64),
    ]
    path = _build_fixture(tmp_path, entries=entries)
    result = parse_pkg(path)
    assert len(result) == 5
    assert [e.name for e in result] == [name for name, _, _ in entries]


def test_parse_pkg_reads_data_offsets_and_sizes(tmp_path):
    entries = [
        ("param.sfo", 16256, 1104),
        ("icon0.png", 25552, 42307),
    ]
    path = _build_fixture(tmp_path, entries=entries)
    result = parse_pkg(path)
    by_name = {e.name: e for e in result}
    assert by_name["param.sfo"].data_offset == 16256
    assert by_name["param.sfo"].data_size == 1104
    assert by_name["icon0.png"].data_offset == 25552
    assert by_name["icon0.png"].data_size == 42307


def test_parse_pkg_returns_immutable_entries(tmp_path):
    path = _build_fixture(tmp_path, entries=[("a.bin", 0, 10)])
    entries = parse_pkg(path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        entries[0].name = "b.bin"  # type: ignore[misc]


# --- sha_prefixes_for_entries --------------------------------------------

def test_sha_prefixes_for_entries_returns_separate_dict(tmp_path):
    """The dict is keyed by entry.index — entries themselves stay frozen."""
    entries_spec = [
        ("a.bin", 0, 8),
        ("b.bin", 16, 8),
    ]
    path = _build_fixture(tmp_path, entries=entries_spec)
    entries = parse_pkg(path)
    # Write two different bodies at the declared offsets so SHA differs.
    with path.open("r+b") as f:
        f.seek(0)
        f.write(b"AAAAAAAA")     # 8 bytes at offset 0
        f.seek(16)
        f.write(b"BBBBBBBB")     # 8 bytes at offset 16
    prefixes = sha_prefixes_for_entries(path, entries)
    # Two different contents → two different prefixes.
    assert prefixes[entries[0].index] != prefixes[entries[1].index]
    # The SHA for "AAAAAAAA" in bytes 0..4 hex = sha256("AAAAAAAA")[:4].
    expected_a = sha256_prefix(b"AAAAAAAA")
    assert prefixes[entries[0].index] == expected_a


def test_sha_prefixes_for_entries_handles_zero_size(tmp_path):
    """Directory entries with data_size == 0 get an empty SHA, no read."""
    entries_spec = [("dir/", 0, 0)]
    path = _build_fixture(tmp_path, entries=entries_spec)
    entries = parse_pkg(path)
    prefixes = sha_prefixes_for_entries(path, entries)
    assert prefixes[entries[0].index] == ""


# --- Fixture parity with upstream ----------------------------------------

def test_real_upstream_pkg_has_5_files():
    """Integration test against the actual upstream pPlay 3.8 PKG.

    This runs only if the upstream PKG is present at the conventional path.
    Skip otherwise (CI may not have it).

    The upstream PKG has 15 file-table entries; 10 are empty-named
    (directory entries) and 5 are real files. The 5 files match what
    our build also emits — proving Bug #18 is NOT a file-table difference.
    """
    upstream = Path("/tmp/pplay-upstream/pplay/IV0001-PPLA00001_00-PPLA000013080000.pkg")
    if not upstream.exists():
        pytest.skip(f"upstream PKG not present at {upstream}")
    entries = parse_pkg(upstream)
    # Filter directory entries (empty name) — keep only the named files.
    named = [e for e in entries if e.name]
    names = sorted(e.name for e in named)
    assert names == [
        "icon0.png",
        "param.sfo",
        "playgo-chunk.dat",
        "playgo-chunk.sha",
        "playgo-manifest.xml",
    ]


def test_real_upstream_pkg_size_matches():
    upstream = Path("/tmp/pplay-upstream/pplay/IV0001-PPLA00001_00-PPLA000013080000.pkg")
    if not upstream.exists():
        pytest.skip(f"upstream PKG not present at {upstream}")
    entries = parse_pkg(upstream)
    # per upstream .PKG header the file has 33,882,112 bytes (the SCE SELF body
    # is 33 MB; the file table only enumerates 5 wrapper files).
    assert upstream.stat().st_size == 33_882_112
    # Param.sfo is 432 B (with 672 of padding to 1104 = 0x450)
    param_sfo = next(e for e in entries if e.name == "param.sfo")
    assert param_sfo.data_size == 1104
    # icon0.png is 42307 B
    icon = next(e for e in entries if e.name == "icon0.png")
    assert icon.data_size == 42307
