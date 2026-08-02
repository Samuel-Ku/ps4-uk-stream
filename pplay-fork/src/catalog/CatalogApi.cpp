#include "CatalogApi.h"
#include "HttpClient.h"
#include "Json.h"
#include "PosterCache.h"

#include <cctype>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace cs {

namespace {

// Minimal RFC 3986 percent-encoding (unreserved chars pass through).
std::string urlEncode(const std::string &s) {
    std::string out;
    out.reserve(s.size());
    for (unsigned char c : s) {
        if (std::isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
            out += static_cast<char>(c);
        } else {
            char buf[4];
            std::snprintf(buf, sizeof(buf), "%%%02X", c);
            out += buf;
        }
    }
    return out;
}

struct Job {
    std::function<void()> fn;
};

} // namespace

// -----------------------------------------------------------------------------
// Impl: owns the worker thread that performs every HTTP call. The HttpClient
// is not thread-safe (Browser has one CURL handle, one response buffer) so
// it must only ever be touched from inside `loop()`.
// -----------------------------------------------------------------------------
class CatalogApi::Impl {
public:
    Impl(std::string baseUrl, std::unique_ptr<HttpClient> http)
        : base_(std::move(baseUrl)), http_(std::move(http)) {
        worker_ = std::thread([this] { loop(); });
    }

    ~Impl() {
        {
            std::lock_guard<std::mutex> lk(m_);
            done_ = true;
        }
        cv_.notify_one();
        if (worker_.joinable()) worker_.join();
    }

    template <typename F>
    void post(F f) {
        {
            std::lock_guard<std::mutex> lk(m_);
            queue_.push_back(Job{std::move(f)});
        }
        cv_.notify_one();
    }

    void httpGet(const std::string &url, const std::function<void(bool ok, std::string body, std::string err)> &cb) {
        std::string err;
        std::string body = http_->get(url, err);
        if (!err.empty() && body.empty()) {
            cb(false, {}, std::move(err));
        } else if (body.empty()) {
            // Empty body, no error string — treat as a network failure.
            cb(false, {}, "error_network");
        } else {
            cb(true, std::move(body), {});
        }
    }

    void httpGetBytes(const std::string &url, const std::function<void(bool ok, std::vector<std::uint8_t> bytes, std::string ct, std::string err)> &cb) {
        std::vector<std::uint8_t> bytes;
        std::string ct;
        std::string err;
        bool ok = http_->getBytes(url, bytes, ct, err);
        if (!ok) err = err.empty() ? std::string("error_network") : std::move(err);
        cb(ok, std::move(bytes), std::move(ct), std::move(err));
    }

    // Accessor used by public async methods to build URLs without poking
    // at private state from outside Impl.
    const std::string &base() const { return base_; }

    // Poster disk cache; set once at startup (see setPosterCacheDir).
    // Worker-thread only, like http_.
    std::unique_ptr<DiskPosterCache> posterCache_;

private:
    void loop() {
        for (;;) {
            Job job;
            {
                std::unique_lock<std::mutex> lk(m_);
                cv_.wait(lk, [this] { return done_ || !queue_.empty(); });
                if (done_ && queue_.empty()) return;
                job = std::move(queue_.front());
                queue_.pop_front();
            }
            job.fn();
        }
    }

    std::string base_;
    std::unique_ptr<HttpClient> http_;
    std::deque<Job> queue_;
    std::mutex m_;
    std::condition_variable cv_;
    bool done_ = false;
    std::thread worker_;
};

// -----------------------------------------------------------------------------
// Public API
// -----------------------------------------------------------------------------

CatalogApi::CatalogApi(std::string baseUrl, std::unique_ptr<HttpClient> http)
    : impl_(new Impl(std::move(baseUrl), std::move(http))) {}

CatalogApi::~CatalogApi() = default;

void CatalogApi::searchAsync(const std::string &query, SearchCb cb) {
    impl_->post([this, query, cb = std::move(cb)]() {
        std::string url = impl_->base() + "/api/search?q=" + urlEncode(query);
        impl_->httpGet(url, [cb](bool ok, std::string body, std::string err) {
            if (!ok) { cb(false, {}, std::move(err)); return; }
            cb(true, parseSearch(body), {});
        });
    });
}

void CatalogApi::contentAsync(const std::string &id, ContentCb cb) {
    impl_->post([this, id, cb = std::move(cb)]() {
        // Backend exposes /api/content/{content_id:path} (path param,
        // not a query string). urlEncode() percent-encodes ':' and '/'
        // in the id, which Starlette decodes back before matching.
        std::string url = impl_->base() + "/api/content/" + urlEncode(id);
        impl_->httpGet(url, [cb](bool ok, std::string body, std::string err) {
            if (!ok) { cb(false, {}, std::move(err)); return; }
            cb(true, parseContent(body), {});
        });
    });
}

void CatalogApi::contentAsyncForSource(const std::string &groupKey,
                                       const std::string &source, ContentCb cb) {
    impl_->post([this, groupKey, source, cb = std::move(cb)]() {
        // /api/content/{group_key:path}?source=<provider>. Backend
        // re-resolves the group under the named provider and returns the
        // source's content_id (which is then assigned to out.id in the
        // parser's synthesized single-entry `sources` list).
        std::string url = impl_->base() + "/api/content/" + urlEncode(groupKey);
        if (!source.empty()) {
            url += "?source=" + urlEncode(source);
        }
        impl_->httpGet(url, [cb](bool ok, std::string body, std::string err) {
            if (!ok) { cb(false, {}, std::move(err)); return; }
            cb(true, parseContent(body), {});
        });
    });
}

void CatalogApi::streamAsync(const std::string &id, const std::string &translation, StreamCb cb) {
    impl_->post([this, id, translation, cb = std::move(cb)]() {
        // /api/stream/{content_id:path}; translation stays a query
        // param because backend main.py defines it that way.
        std::string url = impl_->base() + "/api/stream/" + urlEncode(id);
        if (!translation.empty()) {
            url += "?translation=" + urlEncode(translation);
        }
        impl_->httpGet(url, [cb](bool ok, std::string body, std::string err) {
            if (!ok) { cb(false, {}, std::move(err)); return; }
            cb(true, parseStream(body), {});
        });
    });
}

void CatalogApi::sectionsAsync(SectionsCb cb) {
    impl_->post([this, cb = std::move(cb)]() {
        std::string url = impl_->base() + "/api/sections";
        impl_->httpGet(url, [cb](bool ok, std::string body, std::string err) {
            if (!ok) { cb(false, {}, std::move(err)); return; }
            cb(true, parseSections(body), {});
        });
    });
}

void CatalogApi::browseAsync(const std::string &provider, const std::string &section,
                             int page, BrowseCb cb) {
    impl_->post([this, provider, section, page, cb = std::move(cb)]() {
        std::string url = impl_->base() + "/api/browse?provider=" + urlEncode(provider)
                         + "&section=" + urlEncode(section)
                         + "&page=" + std::to_string(page);
        impl_->httpGet(url, [cb](bool ok, std::string body, std::string err) {
            if (!ok) { cb(false, {}, std::move(err)); return; }
            cb(true, parseBrowse(body), {});
        });
    });
}

void CatalogApi::setPosterCacheDir(std::string dir) {
    // Startup-only call site (Main ctor / test setup), before any poster
    // traffic: direct assignment is safe because no loadPoster job can be
    // queued yet.
    impl_->posterCache_ = std::make_unique<DiskPosterCache>(std::move(dir));
}

void CatalogApi::loadPoster(const std::string &url, PosterCb cb) {
    impl_->post([this, url, cb = std::move(cb)]() {
        if (impl_->posterCache_) {
            std::vector<std::uint8_t> bytes;
            std::string ct;
            if (impl_->posterCache_->get(url, bytes, ct)) {
                cb(true, std::move(bytes), std::move(ct), {});
                return;
            }
        }
        impl_->httpGetBytes(url, [this, url, cb](bool ok, std::vector<std::uint8_t> bytes,
                                                 std::string ct, std::string err) {
            if (ok && impl_->posterCache_) {
                impl_->posterCache_->put(url, bytes, ct);
            }
            cb(ok, std::move(bytes), std::move(ct), std::move(err));
        });
    });
}

// -----------------------------------------------------------------------------
// Parsing (pure, no network).
// -----------------------------------------------------------------------------

std::vector<SearchItem> CatalogApi::parseSearch(const std::string &raw) {
    std::vector<SearchItem> out;
    auto doc = JsonDoc::parse(raw);
    if (!doc) return out;
    for (const auto &v : doc->root().arr("results")) {
        SearchItem it;
        it.id = v.str("id");
        it.provider = v.str("provider");
        it.type = v.str("type");
        it.title = v.str("title");
        it.year = v.integer("year", 0);
        it.poster = v.str("poster");
        it.url = v.str("url");
        if (!it.id.empty()) out.push_back(std::move(it));
    }
    return out;
}

ContentItem CatalogApi::parseContent(const std::string &raw) {
    ContentItem out;
    auto doc = JsonDoc::parse(raw);
    if (!doc) return out;
    auto r = doc->root();
    out.id = r.str("id");
    out.type = r.str("type");
    out.title = r.str("title");
    out.description = r.str("description");
    out.poster = r.str("poster");
    out.groupKey = r.str("group_key");

    // translations_level controls where translations live.
    // "content" (default) → on ContentItem; "episode" → on each Episode.
    std::string level = r.str("translations_level");
    if (level == "episode" || level == "content") {
        out.translationsLevel = level;
    }

    if (out.translationsLevel == "content") {
        for (const auto &t : r.arr("translations")) {
            out.translations.emplace_back(t.str("id"), t.str("label"));
        }
    }

    int seasonNo = 0;
    for (const auto &s : r.arr("seasons")) {
        ContentItem::Season cs2;
        cs2.number = s.integer("number", ++seasonNo);
        int epNo = 0;
        for (const auto &e : s.arr("episodes")) {
            ContentItem::Episode ep;
            ep.number = e.integer("number", ++epNo);
            ep.id = e.str("id");
            ep.title = e.str("title");
            if (out.translationsLevel == "episode") {
                for (const auto &t : e.arr("translations")) {
                    ep.translations.emplace_back(t.str("id"), t.str("label"));
                }
            }
            cs2.episodes.push_back(std::move(ep));
        }
        out.seasons.push_back(std::move(cs2));
    }

    // Issue #62 / v3 spec §3.3: chip-strip roster of every provider that
    // served this group. When the backend omits the field (legacy / single
    // provider / test fixture), synthesize a single-entry roster from `id`
    // so the UI never shows a bogus empty strip.
    for (const auto &src : r.arr("sources")) {
        ContentItem::Source s;
        s.provider = src.str("provider");
        s.id = src.str("id");
        if (!s.provider.empty() && !s.id.empty()) {
            out.sources.push_back(std::move(s));
        }
    }
    if (out.sources.empty() && !out.id.empty()) {
        ContentItem::Source s;
        // Convention: ids are `<provider>:<inner_id>`. Strip the prefix so
        // the chip's backend filter (`?source=<p>`) lines up with the
        // provider id registered on the home / sections response.
        const auto sep = out.id.find(':');
        s.provider = (sep == std::string::npos) ? out.id : out.id.substr(0, sep);
        s.id = out.id;
        out.sources.push_back(std::move(s));
    }
    return out;
}

StreamInfo CatalogApi::parseStream(const std::string &raw) {
    StreamInfo out;
    auto doc = JsonDoc::parse(raw);
    if (!doc) return out;
    auto r = doc->root();
    out.url = r.str("url");
    out.type = r.str("type");
    // headers is an object (key -> value). StreamInfo stores them as
    // a list of pairs so callers can pass them straight to CURL/MPV.
    // We walk root to find the headers sub-object, then iterate its
    // members — JsonValue exposes obj() per-node but not obj(key).
    for (const auto &kv : r.obj()) {
        if (kv.first != "headers") continue;
        for (const auto &header : kv.second.obj()) {
            out.headers.emplace_back(header.first, header.second.str());
        }
    }
    return out;
}

std::vector<ProviderSections> CatalogApi::parseSections(const std::string &raw) {
    std::vector<ProviderSections> out;
    auto doc = JsonDoc::parse(raw);
    if (!doc) return out;
    auto root = doc->root();
    // Accept either {"providers": [...]} or a bare array for robustness.
    auto provArr = root.has("providers") ? root.arr("providers") : root.asArray();
    for (const auto &p : provArr) {
        ProviderSections ps;
        ps.provider = p.str("provider");
        ps.name = p.str("name");
        for (const auto &s : p.arr("sections")) {
            Section sec;
            sec.id = s.str("id");
            sec.title = s.str("title");
            sec.type = s.str("type");
            if (!sec.id.empty()) ps.sections.push_back(std::move(sec));
        }
        out.push_back(std::move(ps));
    }
    return out;
}

BrowseItem CatalogApi::parseBrowse(const std::string &raw) {
    BrowseItem out;
    auto doc = JsonDoc::parse(raw);
    if (!doc) return out;
    auto root = doc->root();
    out.hasNext = root.has("has_next");
    for (const auto &v : root.arr("results")) {
        SearchItem it;
        it.id = v.str("id");
        it.provider = v.str("provider");
        it.type = v.str("type");
        it.title = v.str("title");
        it.year = v.integer("year", 0);
        it.poster = v.str("poster");
        it.url = v.str("url");
        if (!it.id.empty()) out.results.push_back(std::move(it));
    }
    return out;
}

} // namespace cs
