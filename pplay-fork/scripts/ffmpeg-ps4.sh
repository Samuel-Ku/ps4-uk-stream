#!/usr/bin/env bash
set -euo pipefail
: "${OO_PS4_TOOLCHAIN:?OO_PS4_TOOLCHAIN must be set}"
SYSROOT="$OO_PS4_TOOLCHAIN"
# OpenOrbis v0.5.2 only ships the FreeBSD sysroot + PS4 headers/libs +
# pkg tools — it does NOT bundle a cross-prefix gcc. Cross-compile via
# the host clang using --target= so the same CMAKE_TOOLCHAIN_FILE that
# drives the rest of the build (ps4.cmake) is the single source of
# truth. See /opt/oo/cmake/ps4.cmake for the matching flag set.
TARGET="--target=x86_64-pc-freebsd12-elf"
CC="clang"
CXX="clang++"
AR="llvm-ar"
NM="llvm-nm"
RANLIB="llvm-ranlib"
STRIP="llvm-strip"
cd "$(dirname "$0")/.."
mkdir -p external/ffmpeg-ps4 && cd external/ffmpeg-ps4
if [ ! -d ffmpeg ]; then
    echo "==> Cloning FFmpeg n6.1"
    git clone --depth 1 --branch n6.1 https://git.ffmpeg.org/ffmpeg.git
fi
cd ffmpeg
echo "==> Configuring FFmpeg for PS4 (clang ${TARGET})"
# ld flags must match /opt/oo/cmake/ps4.cmake so ffmpeg and the rest of
# the build (libcross2d + pPlay) link the same C runtime files. We use
# `-fuse-ld=${SYSROOT}/linker.sh` so clang `exec()`s our bash wrapper
# for the link (bypassing its FreeBSD driver injection of -L/usr/lib,
# -lgcc*, unqualified crt*.o lookups — see docs/ps4-build-research.md).
# The wrapper adds the canonical OpenOrbis recipe (`-m elf_x86_64 -pie
# --script=link.x --eh-frame-hdr -L=lib`) and forwards the rest. We
# only need to add the archives + crt tail here (same as ps4.cmake).
#
# `-isystem ${SYSROOT}/include` is required for clang to find the
# FreeBSD-target headers (the sysroot has /opt/oo/include not
# /opt/oo/usr/include, so without -isystem clang's default FreeBSD
# sysroot layout fails to locate <math.h>, <stdio.h>, etc.).
#
# Cross-compiling against host libass/freetype/fribidi via pkg-config
# fails: clang with `--target=x86_64-pc-freebsd12-elf --sysroot=/opt/oo`
# treats absolute `-I/usr/include/...` paths from pkg-config as
# sysroot-relative (looking for /opt/oo/usr/include/...). pPlay's
# libcross2d doesn't call libass/freetype/fribidi directly — it goes
# through FFmpeg's libavformat. We only need file/HLS demuxing +
# h264 decode, so disable these and skip the cross-compile dep chain.
#
# Patch FFmpeg's `os_support.h` + `network.h` to add an extra guard
# against double-defining `socklen_t` and `sockaddr_storage`. FFmpeg's
# own HAVE_SOCKLEN_T / HAVE_STRUCT_SOCKADDR_STORAGE config-time checks
# return 0 under our cross-compile (clang's FreeBSD target emits these
# as 0 because the test compilation uses a minimal sysroot stub that
# doesn't include the FreeBSD headers), but the real FreeBSD sysroot
# DOES define both — so we get duplicate-definition errors when
# FFmpeg's headers are included after sys/socket.h. Adding
# `#ifndef socklen_t` / `#ifndef sockaddr_storage` to FFmpeg's
# typedef/struct guards resolves the collision.
sed -i 's@^#if !HAVE_SOCKLEN_T\(.*\)$@#if !HAVE_SOCKLEN_T \&\& !defined(__FreeBSD__)@' \
    libavformat/os_support.h
sed -i 's@^#if !HAVE_STRUCT_SOCKADDR_STORAGE\(.*\)$@#if !HAVE_STRUCT_SOCKADDR_STORAGE \&\& !defined(__FreeBSD__)@' \
    libavformat/network.h

# OpenSSL lives in its own prefix (built by Dockerfile.ps4, #35).
# `--enable-openssl` makes ffmpeg probe libssl/libcrypto via pkg-config;
# without PKG_CONFIG_PATH pointing at the openssl prefix the probe
# fails and https/tls silently configure to 0 (config_components.h).
OPENSSL_PREFIX="${SYSROOT}/openssl"
if [ ! -f "${OPENSSL_PREFIX}/lib/pkgconfig/libssl.pc" ]; then
    echo "==> ERROR: OpenSSL for the PS4 target is not installed at ${OPENSSL_PREFIX}" >&2
    echo "==> Rebuild the ps4-uk-build image (Dockerfile.ps4 builds it)." >&2
    exit 1
fi
export PKG_CONFIG_PATH="${OPENSSL_PREFIX}/lib/pkgconfig${PKG_CONFIG_PATH:+:${PKG_CONFIG_PATH}}"
./configure \
    --prefix=/usr/local \
    --enable-cross-compile --cc="${CC}" --cxx="${CXX}" \
    --ar="${AR}" --nm="${NM}" --ranlib="${RANLIB}" --strip="${STRIP}" \
    --arch=x86_64 \
    --target-os=freebsd --pkg-config=pkg-config \
    --extra-cflags="${TARGET} --sysroot=${SYSROOT} -isystem ${SYSROOT}/include -O2 -fPIC" \
    --extra-ldflags="-fuse-ld=${SYSROOT}/linker.sh -L${SYSROOT}/lib -lc -lkernel -lc++ ${SYSROOT}/lib/crt1.o ${SYSROOT}/lib/crtlib.o" \
    --extra-libs="-L${SYSROOT}/lib -lc -lkernel -lc++" \
    --disable-shared --enable-static --enable-pic \
    --disable-libass --disable-libfreetype --disable-libfribidi \
    --disable-protocols --enable-protocol='file,http,hls,https,tls' \
    --enable-openssl \
    --disable-filters --enable-filter='rotate,transpose' \
    --disable-encoders --disable-muxers --disable-programs \
    --disable-debug --disable-doc --disable-runtime-cpudetect --disable-autodetect
echo "==> Building FFmpeg"
make -j"$(nproc)"
echo "==> Installing FFmpeg to ../ffmpeg-install"
make install DESTDIR="$PWD/../../ffmpeg-install"