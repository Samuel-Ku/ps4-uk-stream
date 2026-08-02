#pragma once

#include "CatalogApi.h"
#include "CatalogState.h"

#include <memory>
#include <string>
#include <unordered_map>

namespace cs {

// Singleton-ish accessor for the single shared CatalogApi instance owned
// by Main. Screens take the pointer in their constructors; the worker
// thread lives inside the api, so callbacks fire on that worker. Screens
// marshal back to the UI thread via std::atomic flags + onUpdate polling
// (libcross2d has no addAction/runOnUiThread helper, and pulling one in
// is out of scope for v2).
//
// CatalogState rides alongside (#55): one store per process, written from
// the UI thread only (playback hand-off + position saver both fire there).
//
// Lifetime: Main constructs the api in its constructor (one place) and
// destructs it before Main::~Main. Screens keep a non-owning pointer.
//
// Why a global pointer instead of plumbing the api through every screen:
//   - the api is genuinely shared singleton state (one worker thread, one
//     HTTP client) — same shape as the existing pplayIo singleton.
//   - keeps the screen constructor signatures minimal and consistent with
//     the existing Main::add(new ScreenSections(this)) call site.
class CatalogContext {
public:
    static void set(std::unique_ptr<CatalogApi> api);
    static CatalogApi *get();

    static void setState(std::unique_ptr<CatalogState> state);
    static CatalogState *state();

    // Issue #62 / #73 — provider health snapshot. The home / sections
    // poll refreshes the map; the chip strip renders "down" chips in a
    // muted shade and refuses to fire refetches on them. Unknown status
    // (provider not yet polled, or missing from the response) is treated
    // as enabled — we'd rather let the user try and see a graceful error
    // than hide a working source behind a stale 'down' snapshot.
    enum class ProviderStatus { Unknown, Up, Degraded, Down };
    static void setProviderStatuses(std::unordered_map<std::string, std::string> statuses);
    static ProviderStatus providerStatus(const std::string &provider);
};

} // namespace cs
