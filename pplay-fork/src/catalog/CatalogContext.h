#pragma once

#include "CatalogApi.h"

#include <memory>

namespace cs {

// Singleton-ish accessor for the single shared CatalogApi instance owned
// by Main. Screens take the pointer in their constructors; the worker
// thread lives inside the api, so callbacks fire on that worker. Screens
// marshal back to the UI thread via std::atomic flags + onUpdate polling
// (libcross2d has no addAction/runOnUiThread helper, and pulling one in
// is out of scope for v2).
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
};

} // namespace cs