#!/usr/bin/env bash
# run-in-ps4-container.sh — the in-container half of the PS4 PKG
# pipeline (issue #32 / Task 19). Executed by
# scripts/build-ps4-docker.sh inside the ps4-uk-build image with the
# pplay-fork tree mounted at /work.
#
# Steps:
#   1. Cross-build FFmpeg (idempotent — skipped when the static
#      libraries already exist from a previous run).
#   2. cmake configure + build pplay for PLATFORM_PS4.
#   3. make pplay.eboot → create-fself → eboot.bin, then make
#      pplay_pkg → PkgTool.Core → IV0000-PPLA00001_00-PPLAY00000000000.pkg.
#   4. Normalise the artifact name to PPLA00001.pkg (the name the
#      manual-install docs reference).
#   5. Validate the artifacts (PKG magic + fself signatures) and print
#      the evidence #35 needs (ffmpeg protocol config).
#
# Exit code 0 = package built and validated.
set -euo pipefail

cd /work
export OO_PS4_TOOLCHAIN="${OO_PS4_TOOLCHAIN:-/opt/oo}"
PKG_OUT="build/IV0000-PPLA00001_00-PPLAY00000000000.pkg"

echo "==> [1/5] FFmpeg for PS4"
NEEDS_FFMPEG=0
[ ! -f external/ffmpeg-install/usr/local/lib/libavformat.a ] && NEEDS_FFMPEG=1
# #35: an install built before the openssl layer has HTTPS/TLS turned
# off — those builds must be reconfigured, or the console loses every
# https stream.
grep -q "CONFIG_HTTPS_PROTOCOL 1" \
    external/ffmpeg-ps4/ffmpeg/config_components.h 2>/dev/null || NEEDS_FFMPEG=1
if [ "$NEEDS_FFMPEG" = "1" ]; then
    bash scripts/ffmpeg-ps4.sh
else
    echo "ffmpeg-install with HTTPS enabled already present — skipping cross-build"
fi

echo "==> [1b] #35 evidence: ffmpeg protocol config"
grep -E "CONFIG_(FILE|HTTP|HTTPS|HLS|TLS)_PROTOCOL " \
    external/ffmpeg-ps4/ffmpeg/config_components.h

echo "==> [2/5] cmake configure"
cmake -B build -S . \
    -DCMAKE_TOOLCHAIN_FILE="$OO_PS4_TOOLCHAIN/cmake/ps4.cmake" \
    -DCMAKE_BUILD_TYPE=Release \
    -DPLATFORM_PS4=ON

echo "==> [3/5] cmake build pplay + eboot.bin"
cmake --build build -j"$(nproc)"
cmake --build build --target pplay.eboot

echo "==> [4/5] cmake build pkg"
cmake --build build --target pplay_pkg
cp "$PKG_OUT" build/PPLA00001.pkg

echo "==> [5/5] Validate artifacts"
# od (coreutils) instead of xxd — the build image has no vim.
MAGIC=$(od -An -tx1 -N4 build/PPLA00001.pkg | tr -d ' \n')
echo "PKG magic: $MAGIC"
# 7f434e54 = \x7FCNT — the PS4 PKG header magic.
if [ "$MAGIC" != "7f434e54" ]; then
    echo "FAIL: PKG magic is $MAGIC, expected 7f434e54" >&2
    exit 1
fi
SELF_MAGIC=$(od -An -tx1 -N4 build/eboot.bin | tr -d ' \n')
echo "eboot.bin magic: $SELF_MAGIC"
# 4f153d1d = SCE SELF wrapper produced by create-fself.
if [ "$SELF_MAGIC" != "4f153d1d" ]; then
    echo "FAIL: eboot.bin magic is $SELF_MAGIC, expected 4f153d1d" >&2
    exit 1
fi
echo "==> readelf on build/pplay.elf (the unsigned ELF — eboot.bin is a"
echo "    SELF container; the OpenOrbis readelf rejects SELF magic)"
"$OO_PS4_TOOLCHAIN/bin/linux/readelf" -h -n build/pplay.elf \
    | tee build/elf-readelf.txt
echo "==> create-fself paid (program auth id) pinned in:"
grep -n "0x3800000000000011" \
    libcross2d/cmake/targets.cmake scripts/ps4-toolchain/pplay-create-fself.sh
echo "==> OK: build/PPLA00001.pkg built and validated"
