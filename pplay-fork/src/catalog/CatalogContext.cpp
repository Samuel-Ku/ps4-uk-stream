#include "CatalogContext.h"

#include <mutex>

namespace cs {

namespace {
std::unique_ptr<CatalogApi> g_api;
std::mutex g_mu;
} // namespace

void CatalogContext::set(std::unique_ptr<CatalogApi> api) {
    std::lock_guard<std::mutex> lk(g_mu);
    g_api = std::move(api);
}

CatalogApi *CatalogContext::get() {
    // No lock: pointer reads of unique_ptr are word-sized and atomic on
    // every platform we target (PS4/ppc64 and amd64). Writers (Main ctor
    // / dtor) happen at well-defined serial points — before any screen
    // touches the api, and after all screens have been removed.
    return g_api.get();
}

} // namespace cs