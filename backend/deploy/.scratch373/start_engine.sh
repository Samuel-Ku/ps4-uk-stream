#!/usr/bin/env bash
# Recreate the #373 scratch engine bootstrapper: run the REAL BitPlay
# engine binary (extracted from ghcr.io/aculix/bitplay, statically
# linked Go) natively — no Docker daemon elevation required — with the
# image's ffmpeg behind PATH shims (here: the host ffmpeg stands in;
# the musl-linked image copy cannot relocate against the host kernel's
# glibc — see the acceptance note).
#
# Recreate the rootfs (crane pull → extract layers):
#   /tmp/crane pull ghcr.io/aculix/bitplay:latest /tmp/bitplay-oci.tar
#   tar xf /tmp/bitplay-oci.tar -C /tmp/bitplay-rootfs
#   # then extract rootfs/*/tar.gz layers sequentially into
#   # /tmp/bitplay-rootfs/rootfs  — see docs/test-artifacts note.
set -uo pipefail
BIN=/tmp/bitplay-rootfs/rootfs/app/main
[ -x "$BIN" ] || { echo "engine binary missing: $BIN"; exit 1; }
mkdir -p /tmp/bitplay-bin /tmp/bitplay-data
for t in ffmpeg ffprobe; do
  printf '#!/bin/sh\nexec %s "$@"\n' "$(command -v $t)" > "/tmp/bitplay-bin/$t"
  chmod +x "/tmp/bitplay-bin/$t"
done
cd /tmp/bitplay-data
export PATH=/tmp/bitplay-bin:$PATH
exec "$BIN"
