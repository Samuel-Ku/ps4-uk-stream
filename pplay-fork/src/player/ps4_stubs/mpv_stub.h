// PS4 stubs for the libmpv client / render APIs that pplay-fork's
// player code references. The OpenOrbisSDK does not ship libmpv, and
// cross-compiling mpv for FreeBSD 12.0 + PS4 is a separate
// project — for Task 16 (produce a PPLA00001.pkg that GoldHEN
// accepts) we just need the build to succeed. The streaming playback
// path that replaces libmpv on PS4 is a follow-up task.
//
// Only the symbols actually used by src/player/{mpv,player,
// video_texture}.{h,cpp} are provided. They're declared as
// opaquely-typed values (struct mpv_handle is forward-declared) so
// the existing pplay code that does mpv_handle *h; or
// mpv_render_context *ctx; still type-checks.

#ifndef PPLAY_PS4_MPV_STUB_H
#define PPLAY_PS4_MPV_STUB_H

#include <cstdint>
#include <cstddef>

// Opaque libmpv types. Defined as forward-declared structs so
// `mpv_handle *` and `mpv_render_context *` work; their real layout
// is only known to libmpv (which we don't link on PS4).
struct mpv_handle;
struct mpv_render_context;
struct mpv_event_end_file;
struct mpv_event {
    int event_id = 0;
    int error = 0;
    void *data = nullptr;
    mpv_event_end_file *end_file = nullptr;
};
struct mpv_opengl_init_params {
    void *(*get_proc_address)(void *ctx, const char *name);
    void *ctx;
};
struct mpv_opengl_fbo {
    int w = 0;
    int h = 0;
    int fbo = 0;
    int tex = 0;
    int internal_format = 0;
};
// mpv_event_end_file: the libmpv payload for MPV_EVENT_END_FILE.
struct mpv_event_end_file {
    int reason = 0;
    int error = 0;
    std::string playlist_entry_id;
};
struct mpv_render_param {
    int type;
    void *data;
};

// libmpv API constants the pplay code references. Values match
// upstream so any comparison / switch on these still behaves.
#define MPV_RENDER_PARAM_API_TYPE            1
#define MPV_RENDER_PARAM_OPENGL_INIT_PARAMS  2
#define MPV_RENDER_PARAM_OPENGL_FBO          3
#define MPV_RENDER_PARAM_FLIP_Y              4
#define MPV_RENDER_PARAM_INVALID             0
#define MPV_RENDER_API_TYPE_OPENGL           "opengl"
#define MPV_RENDER_UPDATE_FRAME              1
#define MPV_ERROR_SUCCESS                     0

// Stubs for the libmpv C entrypoints pplay references. They all return
// safe "not supported" values so the build links and the PS4 runtime
// takes the early-exit branch instead of crashing into a NULL handle.
inline char       *mpv_get_property_string(mpv_handle *, const char *) { return nullptr; }
inline int         mpv_set_option_string(mpv_handle *, const char *, const char *) { return 0; }
inline const char *mpv_error_string(int) { return "mpv disabled on PS4"; }
inline mpv_handle *mpv_create() { return nullptr; }
inline int         mpv_initialize(mpv_handle *) { return MPV_ERROR_SUCCESS; }
inline void        mpv_terminate_destroy(mpv_handle *) {}
inline int         mpv_render_context_create(mpv_render_context **, mpv_handle *, mpv_render_param *) { return MPV_ERROR_SUCCESS; }
inline void        mpv_render_context_free(mpv_render_context *) {}
inline int         mpv_command(mpv_handle *, const char **) { return -1; }
inline int         mpv_command_string(mpv_handle *, const char *) { return -1; }
inline int         mpv_get_property(mpv_handle *, const char *, int, void *) { return -1; }
inline mpv_event  *mpv_wait_event(mpv_handle *, double) { static mpv_event e; return &e; }
inline int         mpv_render_context_update(mpv_render_context *) { return 0; }
inline int         mpv_render_context_render(mpv_render_context *, mpv_render_param *) { return 0; }
inline void       *mpv_get_proc_address(mpv_render_context *, const char *) { return nullptr; }

// MPV_FORMAT_* constants pplay uses in mpv_get_property calls.
#define MPV_FORMAT_NONE      0
#define MPV_FORMAT_DOUBLE    2
#define MPV_FORMAT_INT64     4
#define MPV_FORMAT_FLAG      5
#define MPV_FORMAT_STRING    3
#define MPV_FORMAT_NODE      6
#define MPV_FORMAT_NODE_ARRAY 8
#define MPV_FORMAT_NODE_MAP   9

// Event IDs pplay switches on.
#define MPV_EVENT_START_FILE     8
#define MPV_EVENT_FILE_LOADED    21
#define MPV_EVENT_END_FILE       7

// End-file reason codes.
#define MPV_END_FILE_REASON_ERROR    4
#define MPV_END_FILE_REASON_EOF      0
#define MPV_END_FILE_REASON_QUIT     3
#define MPV_END_FILE_REASON_STOP     2

// mpv_node + helpers used by Mpv::getMediaInfo on the non-PS4 path.
// Declared as a tag-only opaque type with no fields; the PS4
// build of Mpv::getMediaInfo takes the "no streams" early-exit
// because the C-stub mpv_get_property always returns -1, so we
// never actually touch the layout.
struct mpv_node;
struct mpv_node_list { int num; char **keys; mpv_node *values; };
struct mpv_node {
    int format = 0;
    union U {
        char *string;
        int64_t int64;
        double d;
        int flag;
        struct mpv_node_list *list;
    } u;
};

// mpv.cpp declares `extern "C" int ps4_mpv_use_precompiled_shaders;`
// and `ps4_mpv_dump_shaders;` (set inside Mpv::Mpv to control whether
// the PS4 GNM renderer precompiles MPV shaders at build time). On
// the real libmpv those definitions come from the PS4 platform layer
// built into libmpv.a. We don't link libmpv on PS4 (mpv_stub.h
// replaces it), so the actual definitions live in
// src/player/ps4_stubs/mpv_ps4_vars.cpp — included via the file(GLOB)
// in pplay-fork/CMakeLists.txt. The header just declares them so any
// TU that includes <mpv/client.h> can read/write them.
extern "C" int ps4_mpv_use_precompiled_shaders;
extern "C" int ps4_mpv_dump_shaders;

#endif // PPLAY_PS4_MPV_STUB_H
