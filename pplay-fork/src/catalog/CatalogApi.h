#pragma once

#include "HttpClient.h"

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace cs {

struct SearchItem {
    std::string id;
    std::string provider;
    std::string type;
    std::string title;
    int year = 0;
    std::string poster;
    std::string url;
};

struct Section {
    std::string id;
    std::string title;
    std::string type;
};

struct ProviderSections {
    std::string provider;
    std::string name;
    std::vector<Section> sections;
};

struct BrowseItem {
    std::vector<SearchItem> results;
    bool hasNext = false;
};

struct ContentItem {
    std::string id;
    std::string type;
    std::string title;
    std::string description;
    std::string poster;
    // Stateless cross-provider group identity (issue #69): resume/memory
    // records anchor on this, not on the provider-scoped id. Empty when
    // the backend predates the field.
    std::string groupKey;
    // "content" — translations list applies to the whole item
    // "episode" — translations live on each Episode
    std::string translationsLevel = "content";
    std::vector<std::pair<std::string, std::string>> translations;
    struct Episode {
        int number = 0;
        std::string id;
        std::string title;
        // Only populated when translationsLevel == "episode" (anime).
        std::vector<std::pair<std::string, std::string>> translations;
    };
    struct Season {
        int number = 0;
        std::vector<Episode> episodes;
    };
    std::vector<Season> seasons;
    // Issue #62 / v3 spec §3.3: chip-strip roster of every provider that
    // surfaced this group in /api/home. Each entry carries the
    // provider's content id so the chip can drive a refetch
    // (/api/content/{groupKey}?source=<p>) without re-running /api/home.
    // For single-source responses the array has one entry whose id
    // equals `id` above; the chip strip collapses to a no-choice state
    // (single chip, unselectable).
    struct Source {
        std::string provider;
        std::string id;
    };
    std::vector<Source> sources;
};

// Memory policy (issue #74): series-form content is remembered, movies
// never are. Form is keyed on seasons presence — `type` carries a STYLE
// tag ("anime", "cartoon", "dorama"), which is orthogonal to form, so an
// anime MOVIE (style=anime, no seasons) must not be remembered.
inline bool shouldRememberMemory(const ContentItem &item) {
    return !item.seasons.empty();
}

struct ProviderInfo {
    std::string id;
    std::string name;
    // Issue #73 — provider health as reported by /api/providers. Mirrors
    // the backend's HealthStatus literal: "ok" / "degraded" / "down".
    std::string status;
    // Last upstream-error timestamp from the tracker (issue #53). Empty
    // when the provider has not errored in the current tracker window.
    long long lastErrorAt = 0;
};

struct StreamInfo {
    std::string url;
    std::string type;
    std::vector<std::pair<std::string, std::string>> headers;
};

class CatalogApi {
public:
    // Production code wires a Browser-backed HttpClient (see
    // BrowserHttpClient.h). Tests pass a fake. The CatalogApi owns the
    // HttpClient exclusively and runs all calls on its worker thread.
    CatalogApi(std::string baseUrl, std::unique_ptr<HttpClient> http);
    ~CatalogApi();

    // Disable copy: owns a worker thread.
    CatalogApi(const CatalogApi &) = delete;
    CatalogApi &operator=(const CatalogApi &) = delete;

    // Async callbacks — all invoked on the worker thread. UI code is
    // responsible for marshalling to its own thread (see Screen tasks).
    using SearchCb = std::function<void(bool ok, std::vector<SearchItem> results, std::string error)>;
    using ContentCb = std::function<void(bool ok, ContentItem item, std::string error)>;
    using StreamCb = std::function<void(bool ok, StreamInfo info, std::string error)>;
    using SectionsCb = std::function<void(bool ok, std::vector<ProviderSections> providers, std::string error)>;
    using BrowseCb = std::function<void(bool ok, BrowseItem item, std::string error)>;
    using ProvidersCb = std::function<void(bool ok, std::vector<ProviderInfo> providers, std::string error)>;
    using PosterCb = std::function<void(bool ok, std::vector<std::uint8_t> bytes,
                                        std::string contentType, std::string error)>;

    void searchAsync(const std::string &query, SearchCb cb);
    // Issue #63 — provider-scoped variant. When `provider` is non-empty
    // the backend restricts the result set to that provider's catalog.
    void searchAsyncWithProvider(const std::string &query,
                                 const std::string &provider, SearchCb cb);
    void contentAsync(const std::string &id, ContentCb cb);
    // Issue #62: source-filter variant. When `source` is non-empty the
    // backend resolves the same group_key under a different provider and
    // returns that source's `content_id` in the response. The id passed
    // is the group_key (or, on legacy backends, the first provider's id).
    void contentAsyncForSource(const std::string &groupKey, const std::string &source, ContentCb cb);
    void streamAsync(const std::string &id, const std::string &translation, StreamCb cb);
    void sectionsAsync(SectionsCb cb);
    // Issue #73 — provider health snapshot. The chip strip and search
    // filter refresh provider status from this endpoint on every screen
    // load; the result is fed into CatalogContext::setProviderStatuses().
    void providersAsync(ProvidersCb cb);
    void browseAsync(const std::string &provider, const std::string &section,
                     int page, BrowseCb cb);
    void loadPoster(const std::string &url, PosterCb cb);

    // Enable the on-disk poster cache rooted at `dir` (7-day TTL), used by
    // loadPoster before any network fetch. Call once at startup, before the
    // first loadPoster (the cache object is only ever touched on the worker
    // thread, like the HttpClient). Never calling this keeps the legacy
    // always-fetch behaviour.
    void setPosterCacheDir(std::string dir);

    // Pure parsing (testable without network).
    static std::vector<SearchItem> parseSearch(const std::string &raw);
    static ContentItem parseContent(const std::string &raw);
    static StreamInfo parseStream(const std::string &raw);
    static std::vector<ProviderSections> parseSections(const std::string &raw);
    static std::vector<ProviderInfo> parseProviders(const std::string &raw);
    static BrowseItem parseBrowse(const std::string &raw);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace cs
