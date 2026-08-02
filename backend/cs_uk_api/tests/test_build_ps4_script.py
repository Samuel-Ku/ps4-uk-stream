"""Wiring tests for the PS4 PKG build pipeline (issue #32 / spike #35).

Mirrors the test_gate_script.py convention: the bash/docker pipeline is
not executed here (it needs the OpenOrbis image + minutes of compile);
these tests pin the wiring decisions so a future edit cannot silently
drop them:
  - build-ps4-docker.sh must stage the repo-tracked
    run-in-ps4-container.sh (the earlier /tmp/run-in-container.sh copy
    made builds non-reproducible on a clean machine).
  - Dockerfile.ps4 must cross-build OpenSSL for the PS4 target (#35:
    without it FFmpeg configures HTTPS/TLS protocols to 0).
  - ffmpeg-ps4.sh must enable openssl and point PKG_CONFIG_PATH at it.
  - run-in-ps4-container.sh must validate the PKG magic and print the
    ffmpeg protocol-config evidence (#35 closes on that output).
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "Dockerfile.ps4"
BUILD_SH = ROOT / "pplay-fork" / "scripts" / "build-ps4-docker.sh"
RUN_SH = ROOT / "pplay-fork" / "scripts" / "run-in-ps4-container.sh"
FFMPEG_SH = ROOT / "pplay-fork" / "scripts" / "ffmpeg-ps4.sh"


def test_pipeline_files_exist() -> None:
    for path in (DOCKERFILE, BUILD_SH, RUN_SH, FFMPEG_SH):
        assert path.is_file(), f"missing {path}"


def test_build_script_uses_repo_tracked_container_script() -> None:
    text = BUILD_SH.read_text(encoding="utf-8")
    cp_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("cp ") and "run-in-container" in line
    ]
    assert any("pplay-fork/scripts/run-in-ps4-container.sh" in line for line in cp_lines), (
        f"no repo-tracked run-in-ps4-container.sh copy found: {cp_lines}"
    )
    assert not any(line.startswith("cp /tmp/") for line in cp_lines), (
        f"ephemeral /tmp staging still used: {cp_lines}"
    )


def test_dockerfile_cross_builds_openssl_issue35() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "openssl-3.0" in text
    assert "BSD-x86_64" in text
    assert "--prefix=/opt/oo/openssl" in text


def test_ffmpeg_script_enables_openssl_issue35() -> None:
    text = FFMPEG_SH.read_text(encoding="utf-8")
    assert "--enable-openssl" in text
    assert "OPENSSL_PREFIX=" in text
    assert "PKG_CONFIG_PATH" in text


def test_run_in_container_validates_and_reports() -> None:
    text = RUN_SH.read_text(encoding="utf-8")
    # Artifact validation: \x7FCNT PKG magic + SCE SELF magic.
    assert "7f434e54" in text
    assert "4f153d1d" in text
    # #35 evidence: protocol config printed from config_components.h.
    assert "config_components.h" in text
    assert "CONFIG_" in text and "_PROTOCOL" in text
    # readelf runs on the UNSIGNED elf (eboot.bin is a SELF container
    # the OpenOrbis readelf cannot parse).
    assert "readelf" in text
    assert "-h" in text and "-n" in text
    assert "build/pplay.elf" in text
    # paid (program auth id) per the plan's acceptance value.
    assert "0x3800000000000011" in text
