"""Tests for pplay-fork/libcross2d/cmake/gpkg_gen.cmake.

Issue #96: the pkg.gp4 generator must enumerate every file under
${PKG_DIR} (the tree `pplay.pkgtree` staged) instead of hardcoding
the 4-file list. The generator is a `cmake -P`-runnable script so it
can be tested without configuring PLATFORM_PS4 (which requires the
OpenOrbis SDK and isn't possible in the local dev environment — see
scripts/ps4-toolchain/toolchain.cmake:46-55).

The test harness invokes the script via `cmake -P` against tmp trees,
parses the resulting pkg.gp4 as XML, and asserts the schema matches
what PkgTool.Core expects:
- <files> contains one <file targ_path=... orig_path=...> per staged file
- targ_path is RELATIVE to PKG_DIR (not absolute)
- <rootdir> contains a <dir> per top-level subdir
- eboot.bin (flat — the gp4 convention that PkgTool.Core maps to image0) survives
- output is deterministic across re-runs
"""
from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

# pplay-fork/libcross2d/cmake/gpkg_gen.cmake — collocated next to
# pkgtree_stage.cmake so the function form (in targets.cmake) can
# resolve it via CMAKE_CURRENT_LIST_DIR.
GPKG_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "pplay-fork"
    / "libcross2d"
    / "cmake"
    / "gpkg_gen.cmake"
)

# Defaults for the test runs (mirror what add_pkg will pass).
TITLE_ID = "PPLA00001"
TITLE = "pPlay UK Stream"


# --- helpers -------------------------------------------------------------


def _run_gpkg(
    *,
    pkg_dir: Path,
    gp4_path: Path,
    title_id: str = TITLE_ID,
    title: str = TITLE,
) -> subprocess.CompletedProcess:
    """Invoke gpkg_gen.cmake in script mode. Returns the CompletedProcess."""
    if shutil.which("cmake") is None:
        pytest.skip("cmake not on PATH — cannot exercise -P runner")
    return subprocess.run(
        [
            "cmake",
            "-DPKG_DIR=" + str(pkg_dir),
            "-DGP4_PATH=" + str(gp4_path),
            "-DTITLE_ID=" + title_id,
            "-DTITLE=" + title,
            "-P",
            str(GPKG_SCRIPT),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _build_source_fixture(
    tmp_path: Path,
    *,
    subdirs: dict[str, dict[str, bytes]],
) -> Path:
    """Build a fake PPLA00001/ under tmp_path. Returns the PKG_DIR.

    subdirs maps a top-level name (e.g. "sce_sys") to a {relpath: bytes}
    map for the files inside it (use a name like "leaf.bin" for a direct
    file, or "subdir/file.bin" for nested).
    """
    pkg_dir = tmp_path / "PPLA00001"
    pkg_dir.mkdir()
    for top, files in subdirs.items():
        subdir = pkg_dir / top
        if isinstance(files, bytes):
            subdir.write_bytes(files)
            continue
        subdir.mkdir()
        for rel, data in files.items():
            target = subdir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return pkg_dir


# A standard "runtime" tree that mirrors what pplay.pkgtree stages
# after issue #95 lands: 8 files across 5 top-level dirs.
RUNTIME_FIXTURE: dict[str, dict[str, bytes]] = {
    "sce_sys": {
        "param.sfo": b"\x00" * 432,
        "icon0.png": b"\x89PNG",
        "about/right.sprx": b"\x7fELF",
    },
    "sce_module": {
        "libc.prx": b"\x7fELF" + b"libc",
        "libSceFios2.prx": b"\x7fELF" + b"fios2",
    },
    "skin": {
        "btn_play.png": b"\x89PNG" + b"\x00" * 12,
    },
    "mpv": {
        "subfont.ttf": b"OTTO" + b"\x00" * 60,
    },
    "eboot.bin": b"\x4f\x15\x3d\x1d" + b"\x00" * 60,
}


# --- gpkg_gen.cmake exists -----------------------------------------------


def test_gpkg_script_exists():
    """The script MUST exist at the agreed-upon path so the function form
    in targets.cmake can find it via CMAKE_CURRENT_LIST_DIR."""
    assert GPKG_SCRIPT.exists(), f"gpkg_gen.cmake not found at {GPKG_SCRIPT}"


# --- acceptance #1: every staged file is enumerated ---------------------


def test_gpkg_gen_includes_every_file(tmp_path):
    """The generated pkg.gp4 must list every file under PKG_DIR
    (recursively), not a hardcoded subset."""
    pkg_dir = _build_source_fixture(tmp_path, subdirs=RUNTIME_FIXTURE)
    gp4_path = tmp_path / "pkg.gp4"

    result = _run_gpkg(pkg_dir=pkg_dir, gp4_path=gp4_path)
    assert result.returncode == 0, (
        f"gpkg_gen.cmake failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert gp4_path.exists()

    tree = ET.parse(gp4_path)
    files_block = tree.getroot().find("files")
    assert files_block is not None, "<files> block missing"
    entries = files_block.findall("file")
    # 8 expected file entries from RUNTIME_FIXTURE.
    assert len(entries) == 8, f"expected 8 file entries, got {len(entries)}"

    # Every relative path must appear as a targ_path.
    expected_targ_paths = {
        "sce_sys/param.sfo",
        "sce_sys/icon0.png",
        "sce_sys/about/right.sprx",
        "sce_module/libc.prx",
        "sce_module/libSceFios2.prx",
        "skin/btn_play.png",
        "mpv/subfont.ttf",
        "eboot.bin",
    }
    actual_targ_paths = {e.get("targ_path") for e in entries}
    assert actual_targ_paths == expected_targ_paths, (
        f"targ_path set mismatch:\n  expected={expected_targ_paths}\n  actual={actual_targ_paths}"
    )


# --- acceptance #2: targ_path is relative, not absolute -----------------


def test_gpkg_gen_targ_path_is_relative(tmp_path):
    """targ_path must be the path INSIDE the PKG payload (relative to
    PKG_DIR), and orig_path must be the HOST filesystem path. This
    matches what PkgTool.Core expects — see opt/oo/samples/hello_world/pkg.gp4."""
    pkg_dir = _build_source_fixture(tmp_path, subdirs=RUNTIME_FIXTURE)
    gp4_path = tmp_path / "pkg.gp4"

    result = _run_gpkg(pkg_dir=pkg_dir, gp4_path=gp4_path)
    assert result.returncode == 0, result.stderr

    tree = ET.parse(gp4_path)
    entries = tree.getroot().find("files").findall("file")
    for e in entries:
        targ = e.get("targ_path")
        orig = e.get("orig_path")
        # targ_path is a relative path; must NOT start with "/" and must
        # NOT contain the tmp_path prefix.
        assert not targ.startswith("/"), f"targ_path is absolute: {targ!r}"
        assert str(tmp_path) not in targ, f"targ_path leaks tmp dir: {targ!r}"
        # orig_path is the full host path.
        assert str(pkg_dir) in orig, (
            f"orig_path should contain PKG_DIR {pkg_dir}: {orig!r}"
        )
        # And orig_path ends with targ_path (it's PKG_DIR + "/" + targ_path).
        assert orig.endswith(targ), (
            f"orig_path {orig!r} does not end with targ_path {targ!r}"
        )


# --- acceptance #3: eboot.bin flat-path convention ---------------------


def test_gpkg_gen_handles_eboot_flat_path(tmp_path):
    """PkgTool.Core maps the gp4 file with targ_path 'eboot.bin' to
    image0 (the app's main executable). The generator must emit the
    flat targ_path and the orig_path must be the actual host path.
    (A nested 'eboot.bin/eboot.bin' is NOT recognised and produces a
    pkg the console rejects with CE-34629-4 — Bug #18 root cause.)"""
    pkg_dir = _build_source_fixture(
        tmp_path,
        subdirs={"eboot.bin": b"\x4f\x15\x3d\x1d"},
    )
    gp4_path = tmp_path / "pkg.gp4"

    result = _run_gpkg(pkg_dir=pkg_dir, gp4_path=gp4_path)
    assert result.returncode == 0, result.stderr

    tree = ET.parse(gp4_path)
    entries = tree.getroot().find("files").findall("file")
    eboot_entries = [e for e in entries if "eboot" in e.get("targ_path", "")]
    assert len(eboot_entries) == 1, f"expected 1 eboot entry, got {len(eboot_entries)}"
    e = eboot_entries[0]
    assert e.get("targ_path") == "eboot.bin", e.get("targ_path")
    assert e.get("orig_path") == str(pkg_dir / "eboot.bin"), e.get("orig_path")


# --- acceptance #4: <rootdir> reflects top-level subdirs ---------------


def test_gpkg_gen_rootdir_reflects_top_level_dirs(tmp_path):
    """The <rootdir> block must list a <dir targ_name="X"> for every
    top-level subdir under PKG_DIR. Nested dirs (depth 2) get a
    nested <dir>."""
    pkg_dir = _build_source_fixture(tmp_path, subdirs=RUNTIME_FIXTURE)
    gp4_path = tmp_path / "pkg.gp4"

    result = _run_gpkg(pkg_dir=pkg_dir, gp4_path=gp4_path)
    assert result.returncode == 0, result.stderr

    tree = ET.parse(gp4_path)
    rootdir = tree.getroot().find("rootdir")
    assert rootdir is not None, "<rootdir> block missing"
    top_dirs = {d.get("targ_name") for d in rootdir.findall("dir")}
    expected_top_dirs = {"sce_sys", "sce_module", "skin", "mpv"}
    assert top_dirs == expected_top_dirs, (
        f"top-level <dir> mismatch:\n  expected={expected_top_dirs}\n  actual={top_dirs}"
    )

    # sce_sys has an about/ child — verify it nests correctly.
    sce_sys_dir = next(d for d in rootdir.findall("dir") if d.get("targ_name") == "sce_sys")
    nested = {d.get("targ_name") for d in sce_sys_dir.findall("dir")}
    assert "about" in nested, f"sce_sys should have an 'about' child: {nested}"


# --- acceptance #5: deterministic across re-runs ------------------------


def test_gpkg_gen_is_deterministic(tmp_path):
    """Running the script twice with the same inputs must produce a
    byte-identical pkg.gp4 (sorted file enumeration)."""
    pkg_dir = _build_source_fixture(tmp_path, subdirs=RUNTIME_FIXTURE)
    gp4_path_1 = tmp_path / "pkg.gp4.1"
    gp4_path_2 = tmp_path / "pkg.gp4.2"

    res1 = _run_gpkg(pkg_dir=pkg_dir, gp4_path=gp4_path_1)
    res2 = _run_gpkg(pkg_dir=pkg_dir, gp4_path=gp4_path_2)
    assert res1.returncode == 0 and res2.returncode == 0

    content1 = gp4_path_1.read_bytes()
    content2 = gp4_path_2.read_bytes()
    assert content1 == content2, "gpkg_gen.cmake output is non-deterministic"


# --- missing required vars fail fast ------------------------------------


def test_gpkg_gen_fails_fast_without_pkg_dir(tmp_path):
    """PKG_DIR is required. Missing → the script must report it and
    exit nonzero rather than silently writing a half-formed gp4."""
    gp4_path = tmp_path / "pkg.gp4"

    result = subprocess.run(
        ["cmake", "-DGP4_PATH=" + str(gp4_path), "-DTITLE_ID=" + TITLE_ID,
         "-DTITLE=" + TITLE, "-P", str(GPKG_SCRIPT)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0, (
        "gpkg_gen.cmake must exit nonzero when PKG_DIR is empty; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
