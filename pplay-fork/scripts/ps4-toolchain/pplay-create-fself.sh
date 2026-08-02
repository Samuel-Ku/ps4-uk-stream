#!/bin/bash
# Wrapper for create-fself invoked from CMake's add_custom_target.
#
# The cmake Unix Makefiles generator escapes args that look like they
# might contain shell metacharacters by wrapping them in `\"...\"
# sequences. The default /bin/sh on Debian is dash, which doesn't
# process those escapes — it passes the backslashes and quotes through
# literally, so create-fself ends up looking for a file called
# `\"/work/build/pplay\"` instead of `/work/build/pplay`.
#
# To dodge that, this wrapper takes POSITIONAL arguments (cmake leaves
# those unescaped, so the dash /bin/sh escape-quirk is bypassed), with
# defaults matching the only project it is ever invoked for (the
# `pplay` ELF built under /work/build), then execs the real
# create-fself. The defaults exist so the wrapper also works when
# invoked by hand for a smoke test.
#
# See docs/superpowers/plans/2026-08-01-ps4-uk-stream-impl.md.
set -e
OO_PS4_TOOLCHAIN=${OO_PS4_TOOLCHAIN:-/opt/oo}
IN="${1:-/work/build/pplay}"
OUT="${2:-/work/build/eboot/eboot.bin.fself}"
PTYPE="${3:-npdrm_exec}"
# paid = the OpenOrbis-standard homebrew program auth id (also the
# plan's acceptance value, #32).
PAID="${4:-0x3800000000000011}"
# Touch the output dir so the rename-after-create-fself has a target.
mkdir -p "$(dirname "$OUT")"
exec "$OO_PS4_TOOLCHAIN/bin/linux/create-fself" \
    -in="$IN" -out="$OUT" -ptype="$PTYPE" -paid="$PAID"