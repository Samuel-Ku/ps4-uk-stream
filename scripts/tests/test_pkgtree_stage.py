"""Tests for pplay-fork/libcross2d/cmake/pkgtree_stage.cmake.

Issue #95: after `cmake --build build --target pplay.pkgtree`,
`build/PPLA00001/` must contain the runtime files (mpv/subfont.ttf,
sce_module/libc.prx, sce_module/libSceFios2.prx, skin/*, etc.). The
staging logic lives in a `cmake -P`-runnable script so it can be
exercised without configuring PLATFORM_PS4 (which requires the OpenOrbis
SDK and isn't possible in the local dev environment — see
scripts/ps4-toolchain/toolchain.cmake:46-55).

The test harness invokes the script via `cmake -P` against tmp trees and
asserts the resulting layout, idempotency, and overwrite semantics.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# pplay-fork/libcross2d/cmake/pkgtree_stage.cmake — collocated next to
# copy_directory_custom.cmake so the function form
# (in targets.cmake) can resolve it via CMAKE_CURRENT_LIST_DIR.
STAGE_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "pplay-fork"
    / "libcross2d"
    / "cmake"
    / "pkgtree_stage.cmake"
)


# --- helpers -------------------------------------------------------------


def _run_stage(
    *,
    pkg_dir: Path | None,
    romfs_dir: Path,
    datadir_dir: Path,
    eboot_bin: Path,
) -> subprocess.CompletedProcess:
    """Invoke pkgtree_stage.cmake in script mode. Returns the CompletedProcess.

    Each variable is set via -D so the script can read them via CACHE.
    Pass `pkg_dir=None` to omit the `-DPKG_DIR=` flag — used by the
    fails-fast tests to assert the script rejects missing required vars.
    """
    if shutil.which("cmake") is None:
        pytest.skip("cmake not on PATH — cannot exercise -P runner")
    cmd = ["cmake"]
    if pkg_dir is not None:
        cmd += ["-DPKG_DIR=" + str(pkg_dir)]
    cmd += [
        "-DROMFS_DIR=" + str(romfs_dir),
        "-DDATADIR_DIR=" + str(datadir_dir),
        "-DEBOOT_BIN=" + str(eboot_bin),
        "-P",
        str(STAGE_SCRIPT),
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _build_source_fixture(
    tmp_path: Path,
    *,
    romfs_subdirs: dict[str, dict[str, bytes]] | None = None,
    datadir_subdirs: dict[str, dict[str, bytes]] | None = None,
) -> tuple[Path, Path, Path]:
    """Build a fake ROMFS / DATADIR / eboot.bin under tmp_path.

    Returns (romfs_dir, datadir_dir, eboot_bin). Each `subdirs` dict maps
    a top-level name (e.g. "sce_sys") to a {relpath: bytes} map for the
    files inside it (use a name like "leaf.bin" for a direct file, or
    "subdir/file.bin" for nested).
    """
    romfs_dir = tmp_path / "romfs"
    datadir_dir = tmp_path / "datadir"
    eboot_bin = tmp_path / "eboot.bin"
    romfs_dir.mkdir()
    datadir_dir.mkdir()
    eboot_bin.write_bytes(b"\x4f\x15\x3d\x1d" + b"\x00" * 60)  # fake SELF magic

    for top, files in (romfs_subdirs or {}).items():
        subdir = romfs_dir / top
        subdir.mkdir()
        for rel, data in files.items():
            target = subdir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    for top, files in (datadir_subdirs or {}).items():
        subdir = datadir_dir / top
        subdir.mkdir()
        for rel, data in files.items():
            target = subdir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    return romfs_dir, datadir_dir, eboot_bin


# --- pkgtree_stage.cmake exists ------------------------------------------


def test_stage_script_exists():
    """The script MUST exist at the agreed-upon path so the function form
    in targets.cmake can find it via CMAKE_CURRENT_LIST_DIR."""
    assert STAGE_SCRIPT.exists(), (
        f"pkgtree_stage.cmake not found at {STAGE_SCRIPT}"
    )


# --- acceptance #1: pkgtree stages all the named files ------------------


def test_pkgtree_stages_named_files(tmp_path):
    """After staging, PPLA00001/ must contain:
      - mpv/subfont.ttf           (data/common/datadir)
      - sce_module/libc.prx       (data/ps4/romfs)
      - sce_module/libSceFios2.prx
      - skin/btn_play.png         (data/common/romfs)
      - sce_sys/param.sfo         (data/ps4/romfs)
      - sce_sys/icon0.png
      - sce_sys/about/right.sprx
      - eboot.bin                (flat — the PkgTool.Core gp4 convention)
    """
    romfs_files = {
        "sce_sys": {
            "param.sfo": b"\x00" * 432,
            "icon0.png": b"\x89PNG",
            "about/right.sprx": b"\x7fELF",
        },
        "sce_module": {
            "libc.prx": b"\x7fELF" + b"libc",
            "libSceFios2.prx": b"\x7fELF" + b"fios2",
        },
    }
    datadir_files = {
        "mpv": {"subfont.ttf": b"OTTO" + b"\x00" * 60},
    }
    skin_files = {
        "btn_play.png": b"\x89PNG" + b"\x00" * 12,
    }
    # Skin lives under romfs in the real source tree (data/common/romfs/skin).
    romfs_files["skin"] = skin_files

    romfs, datadir, eboot = _build_source_fixture(
        tmp_path,
        romfs_subdirs=romfs_files,
        datadir_subdirs=datadir_files,
    )
    pkg_dir = tmp_path / "PPLA00001"

    result = _run_stage(
        pkg_dir=pkg_dir,
        romfs_dir=romfs,
        datadir_dir=datadir,
        eboot_bin=eboot,
    )
    assert result.returncode == 0, (
        f"pkgtree_stage.cmake failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    expected = [
        pkg_dir / "sce_sys" / "param.sfo",
        pkg_dir / "sce_sys" / "icon0.png",
        pkg_dir / "sce_sys" / "about" / "right.sprx",
        pkg_dir / "sce_module" / "libc.prx",
        pkg_dir / "sce_module" / "libSceFios2.prx",
        pkg_dir / "mpv" / "subfont.ttf",
        pkg_dir / "skin" / "btn_play.png",
        pkg_dir / "eboot.bin",
    ]
    for path in expected:
        assert path.exists(), f"missing: {path}"
        assert path.is_file(), f"not a regular file: {path}"
    # eboot.bin must have the original eboot bytes
    assert (pkg_dir / "eboot.bin").read_bytes() == eboot.read_bytes()


# --- acceptance #2: no extra files leak into sce_sys --------------------


def test_pkgtree_does_not_overwrite_sce_sys(tmp_path):
    """Pre-existing files under PPLA00001/sce_sys/ must be wiped —
    no extra files leak into the destination's sce_sys/ from a
    previous build.

    (Acceptance: "No file is staged under `build/PPLA00001/sce_sys/`
    other than what `data_romfs/sce_sys/` already provides".)
    """
    romfs_files = {
        "sce_sys": {
            "param.sfo": b"\x00" * 100,
        },
    }
    romfs, datadir, eboot = _build_source_fixture(
        tmp_path,
        romfs_subdirs=romfs_files,
    )
    pkg_dir = tmp_path / "PPLA00001"
    # Pre-create a stale tree with an extra file in sce_sys/ that should
    # not survive the staging.
    (pkg_dir / "sce_sys").mkdir(parents=True)
    (pkg_dir / "sce_sys" / "stale_extra_file.txt").write_text("must be removed")
    (pkg_dir / "should_be_wiped_too.bin").write_text("entire dest tree replaced")

    result = _run_stage(
        pkg_dir=pkg_dir,
        romfs_dir=romfs,
        datadir_dir=datadir,
        eboot_bin=eboot,
    )
    assert result.returncode == 0, result.stderr
    assert (pkg_dir / "sce_sys" / "param.sfo").exists()
    assert not (pkg_dir / "sce_sys" / "stale_extra_file.txt").exists()
    assert not (pkg_dir / "should_be_wiped_too.bin").exists()


# --- acceptance #3: idempotent ------------------------------------------


def test_pkgtree_is_idempotent(tmp_path):
    """Re-running the script must produce a byte-identical tree (ignoring mtimes).

    Snapshots the staged tree from run #1 to a separate location, runs the
    script a second time, and compares the two resulting trees — NOT a
    single tree compared to itself, which would be a tautology.
    """
    romfs_files = {"sce_sys": {"param.sfo": b"\x00" * 432}}
    datadir_files = {"mpv": {"subfont.ttf": b"OTTO" + b"\x00" * 60}}
    romfs, datadir, eboot = _build_source_fixture(
        tmp_path,
        romfs_subdirs=romfs_files,
        datadir_subdirs=datadir_files,
    )
    pkg_dir = tmp_path / "PPLA00001"
    pkg_dir_2 = tmp_path / "PPLA00001-snapshot"

    res1 = _run_stage(pkg_dir=pkg_dir, romfs_dir=romfs, datadir_dir=datadir, eboot_bin=eboot)
    # Copy the first run's tree aside so the second run (which wipes
    # pkg_dir before staging) doesn't destroy the comparator.
    shutil.copytree(pkg_dir, pkg_dir_2)
    res2 = _run_stage(pkg_dir=pkg_dir, romfs_dir=romfs, datadir_dir=datadir, eboot_bin=eboot)
    assert res1.returncode == 0 and res2.returncode == 0

    # Walk both trees; assert filenames and SHA256 match exactly.
    def _walk(p: Path):
        rels = []
        for f in sorted(p.rglob("*")):
            if f.is_file():
                rels.append((f.relative_to(p), _sha256(f)))
        return rels

    assert _walk(pkg_dir) == _walk(pkg_dir_2)


def _sha256(p: Path) -> str:
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()


# --- acceptance: handles missing source dirs ----------------------------


def test_pkgtree_handles_missing_data_dirs(tmp_path):
    """When datadir/ is absent, mpv/ should be absent in the dest.
    Empty romfs → only eboot.bin lands in dest.
    """
    romfs = tmp_path / "romfs"
    romfs.mkdir()
    datadir = tmp_path / "datadir"  # do NOT create — must be missing
    eboot = tmp_path / "eboot.bin"
    eboot.write_bytes(b"\x00" * 64)

    pkg_dir = tmp_path / "PPLA00001"
    result = _run_stage(
        pkg_dir=pkg_dir,
        romfs_dir=romfs,
        datadir_dir=datadir,
        eboot_bin=eboot,
    )
    assert result.returncode == 0, result.stderr
    # Only eboot.bin should be present.
    files = [p.relative_to(pkg_dir) for p in pkg_dir.rglob("*") if p.is_file()]
    assert files == [Path("eboot.bin")], files


# --- missing required vars fail fast ------------------------------------


def test_pkgtree_fails_fast_without_pkg_dir(tmp_path):
    """PKG_DIR is required. Missing → the script must report it and
    exit nonzero rather than silently creating files in the cwd."""
    romfs = tmp_path / "romfs"
    romfs.mkdir()
    datadir = tmp_path / "datadir"
    datadir.mkdir()
    eboot = tmp_path / "eboot.bin"
    eboot.write_bytes(b"\x00" * 64)

    # Reuse _run_stage with pkg_dir=None so the cmake PATH skip and
    # timeout handling stay in one place (DRY).
    result = _run_stage(
        pkg_dir=None,
        romfs_dir=romfs,
        datadir_dir=datadir,
        eboot_bin=eboot,
    )
    assert result.returncode != 0, (
        "pkgtree_stage.cmake must exit nonzero when PKG_DIR is empty; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
