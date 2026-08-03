// Definitions for ps4_mpv_* globals declared in mpv_stub.h.
//
// The real libmpv ships these as part of its PS4 platform layer (see
// upstream ps4/mpv.c in libmpv). We don't link libmpv on PS4, so
// provide them here. mpv.cpp's Mpv constructor writes to them on
// startup; we initialise to the values that upstream picks when the
// binary doesn't ship precompiled shaders (the build we are
// targeting — see scripts/ffmpeg-ps4.sh which disables libass /
// freetype to skip the shacc path).
//
// Kept in a dedicated .cpp so the symbol is defined exactly once,
// regardless of how many translation units include <mpv/client.h>
// (which on PS4 pulls in mpv_stub.h transitively).

extern "C" int ps4_mpv_use_precompiled_shaders = 0;
extern "C" int ps4_mpv_dump_shaders = 0;