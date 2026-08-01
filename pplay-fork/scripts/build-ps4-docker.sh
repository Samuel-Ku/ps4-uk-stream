#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# Allow `docker` or `podman`; default to docker, fall back to podman.
if command -v docker >/dev/null 2>&1; then
    RUNTIME=docker
elif command -v podman >/dev/null 2>&1; then
    RUNTIME=podman
else
    echo "ERROR: neither docker nor podman is installed" >&2
    exit 1
fi

echo "==> Building PS4 UK Stream PKG using $RUNTIME"
$RUNTIME build -t ps4-uk-build -f "$ROOT/Dockerfile.ps4" "$ROOT"

$RUNTIME run --rm -v "$ROOT/pplay-fork":/work ps4-uk-build \
  "set -euo pipefail; \
   cd /work; \
   export OO_PS4_TOOLCHAIN=/opt/oo; export OPENORBIS=/opt/oo; \
   echo '==> Building FFmpeg for PS4'; \
   bash scripts/ffmpeg-ps4.sh; \
   echo '==> Configuring pPlay with PLATFORM_PS4=ON'; \
   cmake -B build -DPLATFORM_PS4=ON -DCMAKE_BUILD_TYPE=Release; \
   echo '==> Building pPlay'; \
   cmake --build build -- -j; \
   echo '==> Listing PS4 build artifacts'; \
   ls -la build/eboot.bin build/param.sfo build/PPLA00001.pkg 2>/dev/null || \
     (echo 'Build did not produce all expected artifacts; see build log.' && exit 1)"