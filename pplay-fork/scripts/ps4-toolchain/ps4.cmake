set(CMAKE_SYSTEM_NAME Orbis)
set(CMAKE_SYSTEM_PROCESSOR x86_64)

set(CMAKE_C_COMPILER clang)
set(CMAKE_CXX_COMPILER clang++)

# Use a thin bash wrapper to drive ld.lld, invoked via cmake's
# `CMAKE_C_USE_LINKER` / `CMAKE_CXX_USE_LINKER`. cmake 3.16 forwards
# these to clang as `-fuse-ld=<value>`, which makes clang `exec()` our
# wrapper for the link step — bypassing its FreeBSD driver injection
# of `-L/usr/lib`, `-lgcc*`, and the implicit unqualified `crt1.o /
# crti.o / crtbegin.o / crtend.o / crtn.o` lookups that all fail under
# our /opt/oo sysroot. The wrapper adds the canonical OpenOrbis recipe
# (`-m elf_x86_64 -pie --script=link.x --eh-frame-hdr -L=lib`) and
# forwards the rest. See docs/ps4-build-research.md for the full chain
# of errors that surface otherwise.
set(CMAKE_C_USE_LINKER   ${OPENORBIS}/linker.sh)
set(CMAKE_CXX_USE_LINKER ${OPENORBIS}/linker.sh)
set(CMAKE_LINKER         ${OPENORBIS}/linker.sh)

set(CMAKE_FIND_ROOT_PATH ${OPENORBIS})

set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
# Our FFmpeg install lives at /work/external/ffmpeg-install/usr/local
# (scripts/ffmpeg-ps4.sh writes DESTDIR=$PWD/../../ffmpeg-install
# relative to external/ffmpeg-ps4/ffmpeg/), NOT inside /opt/oo. The
# OpenOrbis toolchain recipes all live there, but for our local
# cross-built deps (FFmpeg today, anything else tomorrow) we want
# cmake to search the sysroot *and* the work tree. BOTH is the
# standard cross-compile escape hatch for that.
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY BOTH)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE BOTH)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE BOTH)

# Canonical OpenOrbis link recipe — matches samples/hello_world/Makefile
# from OpenOrbis-PS4-Toolchain v0.5.2. Order matters: archives
# (-lc -lkernel -lc++) BEFORE the crt tail so ld.lld resolves
# undefined refs out of the archives first, then crt1.o provides
# _start / environ / __progname and crtlib.o provides
# .data.sce_process_param + _init/_fini for create-fself.
#
# `-fuse-ld=` MUST appear directly in CMAKE_EXE_LINKER_FLAGS (not
# only CMAKE_C_USE_LINKER): cmake's compiler-test phase runs its
# own probe link that doesn't honor CMAKE_<LANG>_USE_LINKER, so the
# wrapper needs to be requested via the link-flags path to make
# `cmake -B build` happy. The wrapper also adds
# `-m elf_x86_64 -pie --script=link.x --eh-frame-hdr -L=lib`
# unconditionally, so we don't include them here.
set(CMAKE_C_FLAGS "--target=x86_64-pc-freebsd12-elf -fPIC -funwind-tables -isysroot ${OPENORBIS} -isystem ${OPENORBIS}/include")
# -include cstdlib -include cmath-shim.h: clang-10 + the FreeBSD 12.0
# libc++ in this sysroot has two rough edges that any pre-C++20 source
# trips over:
#   1. <cmath> declares `using ::abs;` without transitively pulling
#      in <cstdlib>, so a TU that includes <cmath> first fails with
#      "no member named 'abs' in the global namespace".
#   2. The shipped <cmath> / <math.h> only expose isnan / isinf as
#      `#define` macros, not as functions in std:: or in the global
#      namespace — so any pre-C++20 code (libcross2d sfml/ widgets,
#      glm, libc++'s <random>) that does `using std::isnan;` or
#      `return std::isinf(x);` fails to resolve. The Dockerfile ships
#      cmath-shim.h which injects inline overloads of isnan / isinf
#      for float / double / long double into both std:: and the
#      global namespace, backed by __builtin_isnan / __builtin_isinf.
# Pre-include both — a few hundred bytes of declarations, and it
# unblocks all of libcross2d + glm.
set(CMAKE_CXX_FLAGS "--target=x86_64-pc-freebsd12-elf -fPIC -funwind-tables -isysroot ${OPENORBIS} -isystem ${OPENORBIS}/include -isystem ${OPENORBIS}/include/c++/v1 -include cstdlib -include cmath-shim.h")
# SDL2.a (vendored under /opt/oo/lib, picked up via pkg-config) makes
# unresolved references into the Sony SDK: scePad* → libScePad,
# sceVideoOut* → libSceVideoOut, sceAudioOut* → libSceAudioOut,
# sceUserService* → libSceUserService. The .so files under /opt/oo/lib
# are the dynamic stubs that the PS4 firmware resolves at runtime, so
# linking them just satisfies ld.lld. Without these the link fails
# with errors like `undefined symbol: scePadInit`.
set(CMAKE_EXE_LINKER_FLAGS "-fuse-ld=${OPENORBIS}/linker.sh -L${OPENORBIS}/lib -lc -lkernel -lc++ ${OPENORBIS}/lib/crt1.o ${OPENORBIS}/lib/crtlib.o -lScePad -lSceAudioOut -lSceVideoOut -lSceUserService -lSceSysmodule -lSceSystemService")
set(CMAKE_SHARED_LINKER_FLAGS "-fuse-ld=${OPENORBIS}/linker.sh -L${OPENORBIS}/lib -lc -lkernel -lc++ ${OPENORBIS}/lib/crt1.o ${OPENORBIS}/lib/crtlib.o -lScePad -lSceAudioOut -lSceVideoOut -lSceUserService -lSceSysmodule -lSceSystemService")
