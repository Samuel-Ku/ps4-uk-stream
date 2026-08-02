#include "CatalogContext.h"

#include <mutex>
#include <utility>

namespace cs {

namespace {
std::unique_ptr<CatalogApi> g_api;
std::unique_ptr<CatalogState> g_state;
std::unordered_map<std::string, std::string> g_providerStatuses;
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

void CatalogContext::setState(std::unique_ptr<CatalogState> state) {
    std::lock_guard<std::mutex> lk(g_mu);
    g_state = std::move(state);
}

CatalogState *CatalogContext::state() {
    // Same lifetime argument as ::get().
    return g_state.get();
}

void CatalogContext::setProviderStatuses(std::unordered_map<std::string, std::string> statuses) {
    std::lock_guard<std::mutex> lk(g_mu);
    g_providerStatuses = std::move(statuses);
}

CatalogContext::ProviderStatus CatalogContext::providerStatus(const std::string &provider) {
    std::lock_guard<std::mutex> lk(g_mu);
    auto it = g_providerStatuses.find(provider);
    if (it == g_providerStatuses.end()) return ProviderStatus::Unknown;
    if (it->second == "ok" || it->second == "up") return ProviderStatus::Up;
    if (it->second == "degraded") return ProviderStatus::Degraded;
    if (it->second == "down") return ProviderStatus::Down;
    return ProviderStatus::Unknown;
}

} // namespace cs
