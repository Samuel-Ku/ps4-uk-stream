#!/usr/bin/env bash
# Build a PS4 PS4-UK-Stream .pkg (PPLA00001.pkg) using the
# openorbisofficial/toolchain-derived ps4-uk-build image.
#
# The Dockerfile layers the OpenOrbis v0.5.2 cmake glue (ps4.cmake)
# + the patched libc.a + crtlib.o on top of
# `openorbisofficial/toolchain:latest`. See docs/ps4-build-research.md
# for the full link-recipe rationale and why the
# freebsd-crt/ + ld-driver.sh detour was wrong.
set -euo pipefail
# Resolve this script's absolute path so $0 stays valid even when
# invoked via a relative path (e.g. `bash scripts/build-ps4-docker.sh`
# from `ps4-uk-stream/`). ROOT = the monorepo root (two levels above
# this script), so we can `cd $ROOT/pplay-fork`.
SCRIPT_PATH="$(readlink -f "$0")"
ROOT="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
# Allow `docker` or `podman`; default to docker, fall back to podman.
if command -v docker >/dev/null 2>&1; then
    RUNTIME=docker
elif command -v podman >/dev/null 2>&1; then
    RUNTIME=podman
else
    echo "ERROR: neither docker nor podman is installed" >&2
    exit 1
fi

# podman's COPY resolver truncates the first path component when the
# build context contains a literal space (a known issue with the
# UID/GID chown checks inside copier). Stage the Dockerfile and the
# ps4.cmake it COPYs into a /tmp dir whose path has no spaces, build
# from there. The actual source tree stays at $ROOT.
BUILD_CTX="$(mktemp -d -t ps4-build-XXXXXX)"
trap 'rm -rf "$BUILD_CTX"' EXIT
cp "$ROOT/Dockerfile.ps4" "$BUILD_CTX/Dockerfile"
mkdir -p "$BUILD_CTX/pplay-fork/scripts/ps4-toolchain"
cp "$ROOT/pplay-fork/scripts/ps4-toolchain/ps4.cmake"  "$BUILD_CTX/pplay-fork/scripts/ps4-toolchain/"
cp "$ROOT/pplay-fork/scripts/ps4-toolchain/linker.sh" "$BUILD_CTX/pplay-fork/scripts/ps4-toolchain/"

echo "==> Building PS4 image (Dockerfile.ps4) using $RUNTIME"
# --load is required so the built image is registered in the local image
# store (the default docker-container driver otherwise leaves it only in
# the build cache, and `docker run` can't find it).
if [ "$RUNTIME" = "docker" ]; then
    "$RUNTIME" build --load -t ps4-uk-build -f "$BUILD_CTX/Dockerfile" "$BUILD_CTX"
else
    "$RUNTIME" build -t ps4-uk-build -f "$BUILD_CTX/Dockerfile" "$BUILD_CTX"
fi

echo "==> Building PS4 PKG inside ps4-uk-build"
# Use a script file (not an inline -c) so multi-line bash + newlines
# survive the host->container boundary without ANSI-C quoting issues.
# The container-side pipeline is repo-tracked (run-in-ps4-container.sh);
# the earlier /tmp/run-in-container.sh staging made builds
# non-reproducible on a clean machine (issue #32).
cp "$ROOT/pplay-fork/scripts/run-in-ps4-container.sh" "$BUILD_CTX/run-in-container.sh"
"$RUNTIME" run --rm \
    -v "$ROOT/pplay-fork":/work \
    -v "$ROOT":/repo \
    -v "$BUILD_CTX/run-in-container.sh":/run-in-container.sh:ro \
    --entrypoint /bin/bash \
    ps4-uk-build \
    /run-in-container.sh
