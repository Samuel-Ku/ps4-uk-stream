#!/usr/bin/env bash
set -euo pipefail
: "${OO_PS4_TOOLCHAIN:?OO_PS4_TOOLCHAIN must be set}"
SYSROOT="$OO_PS4_TOOLCHAIN"
CROSS="x86_64-pc-freebsd12-elf-"
cd "$(dirname "$0")/.."
mkdir -p external/ffmpeg-ps4 && cd external/ffmpeg-ps4
if [ ! -d ffmpeg ]; then
    echo "==> Cloning FFmpeg n6.1"
    git clone --depth 1 --branch n6.1 https://git.ffmpeg.org/ffmpeg.git
fi
cd ffmpeg
echo "==> Configuring FFmpeg for PS4 (${CROSS})"
./configure \
    --prefix=/usr/local \
    --enable-cross-compile --cross-prefix="${CROSS}" --arch=x86_64 \
    --target-os=freebsd --pkg-config=pkg-config \
    --extra-cflags="--sysroot=${SYSROOT} -O2 -fPIC" \
    --extra-ldflags="--sysroot=${SYSROOT} -fPIC" \
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