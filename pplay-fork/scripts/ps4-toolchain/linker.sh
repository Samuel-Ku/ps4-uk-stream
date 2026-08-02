#!/usr/bin/env bash
# linker.sh — invoked by clang via `-fuse-ld=<this>` (see ps4.cmake).
#
# clang 10, when targeting `--target=x86_64-pc-freebsd12-elf`, does NOT
# use the FreeBSD driver's unqualified crt1.o lookups (those need
# `-fuse-ld=lld` to be replaced with `-m elf_x86_64` etc.). When the
# linker is *our* wrapper (`-fuse-ld=/opt/oo/linker.sh`), clang falls
# back to its DEFAULT Linux driver — which injects a long list of
# args that all fail under our /opt/oo sysroot:
#
#   -z relro
#   --hash-style=gnu
#   --build-id
#   -m elf_x86_64
#   -dynamic-linker /lib64/ld-linux-x86-64.so.2   (Linux dynamic linker)
#   /usr/bin/../lib/gcc/x86_64-linux-gnu/9/.../crt1.o   (host glibc crt1.o)
#   /usr/bin/../lib/gcc/x86_64-linux-gnu/9/.../crti.o   (host glibc crti.o)
#   /usr/bin/../lib/gcc/x86_64-linux-gnu/9/crtbegin.o   (host glibc)
#   -L/usr/bin/../lib/gcc/x86_64-linux-gnu/9/...   (host gcc lib search)
#   -L/usr/lib/x86_64-linux-gnu                   (host glibc search)
#   -lc (the host's glibc)
#   -lgcc --as-needed -lgcc_s --no-as-needed      (gcc runtime; not in
#                                                   Sony SDK libc.a)
#   /usr/bin/../lib/gcc/x86_64-linux-gnu/9/.../crtend.o   (host glibc)
#   /usr/bin/../lib/gcc/x86_64-linux-gnu/9/.../crtn.o     (host glibc)
#
# This wrapper:
#   1. Strips every one of the above (and their companions).
#   2. Adds the canonical OpenOrbis recipe (`-m elf_x86_64 -pie
#      --eh-frame-hdr --script=link.x -L=lib`).
#   3. Forwards the rest (input objects, -o output, -lc, -lkernel,
#      -lc++, our explicit /opt/oo/lib/crt1.o + /opt/oo/lib/crtlib.o).
#
# Result: ld.lld gets a clean OpenOrbis recipe and links cleanly.
#
# See docs/ps4-build-research.md for the full chain of errors this
# resolves, and the upstream OpenOrbis samples/hello_world/Makefile
# for the canonical recipe.
set -euo pipefail
OO="${OO_PS4_TOOLCHAIN:-/opt/oo}"
LIB="$OO/lib"
LINK="$OO/link.x"

# Strip the clang-injected Linux / glibc / gcc flags. Walk the args once
# and rebuild the array in place.
ARGS=()
SKIP_NEXT=0
for arg in "$@"; do
    if [ "$SKIP_NEXT" = "1" ]; then
        SKIP_NEXT=0
        continue
    fi
    case "$arg" in
        # clang injected (1-arg, value follows).
        -dynamic-linker|--dynamic-linker|-z|--build-id|-m|--build-id=*|-z,*)  SKIP_NEXT=1 ;;

        # clang injected gcc search roots + glibc search roots.
        -L/usr/bin/*|-L/usr/lib*|-L/lib/x86_64-linux-gnu|-L/lib/../lib64|-L/usr/lib/llvm-*) ;;

        # clang injected full-path crt files (would clobber our /opt/oo
        # versions with the host's glibc equivalents). Strip ONLY crt
        # files outside /opt/oo/lib/ — keep the ones in our sysroot.
        # Order matters: /opt/oo/* must be checked BEFORE the generic
        # `*crt*.o` patterns, otherwise the keep-list below loses.
        /opt/oo/*)            ARGS+=("$arg") ;;  # KEEP our sysroot files
        *crt1.o|*crti.o|*crtbegin*.o|*crtend*.o|*crtn.o|*Scrt1.o|*gcrt1.o) ;;

        # clang injected gcc runtime.
        -lgcc|-lgcc_s|-lgcc_eh|--as-needed|--no-as-needed) ;;

        # clang injected defaults — irrelevant for PS4 static PIE.
        --hash-style=*)               ;;
        --enable-new-dtags|--disable-new-dtags) ;;
        --eh-frame-hdr)               ARGS+=("$arg") ;;  # keep, link.x needs it
        --build-id|--build-id=*)      ;;

        # Keep the user-supplied args + the rest.
        *)
            ARGS+=("$arg")
            ;;
    esac
done

exec /usr/bin/ld.lld \
    -m elf_x86_64 \
    -pie \
    --eh-frame-hdr \
    --script="$LINK" \
    -L"$LIB" \
    "${ARGS[@]}"
