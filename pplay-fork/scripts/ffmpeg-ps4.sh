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
# the build (libcross2d + pPlay) link the same C runtime files. crt1.o
# is passed positionally because the linker doesn't search --sysroot
# in the same way Clang does.
./configure \
    --prefix=/usr/local \
    --enable-cross-compile --cc="${CC}" --cxx="${CXX}" \
    --ar="${AR}" --nm="${NM}" --ranlib="${RANLIB}" --strip="${STRIP}" \
    --arch=x86_64 \
    --target-os=freebsd --pkg-config=pkg-config \
    --extra-cflags="${TARGET} --sysroot=${SYSROOT} -O2 -fPIC -isysroot ${SYSROOT}" \
    --extra-ldflags="${TARGET} --sysroot=${SYSROOT} -fuse-ld=lld -L${SYSROOT}/lib -lc -lkernel -lc++ ${SYSROOT}/lib/crt1.o" \
    --extra-libs="-L${SYSROOT}/lib -lc -lkernel -lc++" \
    --disable-shared --enable-static --enable-pic \
    --enable-libass --enable-libfreetype --enable-libfribidi \
    --disable-protocols --enable-protocol='file,http,hls,https,tls' \
    --disable-filters --enable-filter='rotate,transpose' \
    --disable-encoders --disable-muxers --disable-programs \
    --disable-debug --disable-doc --disable-runtime-cpudetect --disable-autodetect
echo "==> Building FFmpeg"
make -j"$(nproc)"
echo "==> Installing FFmpeg to ../ffmpeg-install"
make install DESTDIR="$PWD/../../ffmpeg-install"