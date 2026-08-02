// Test CatalogApi's worker-thread async surface using a FakeHttpClient.
// No network, no Browser — the production HttpClient is in
// BrowserHttpClient.cpp (not compiled here). Style matches the existing
// test_catalog_api.cpp (assert macros, return code).

#include "../standalone-catalog/FakeHttpClient.h"

#include "CatalogApi.h"
#include "HttpClient.h"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

namespace {

int g_passed = 0;
int g_failed = 0;

#define CHECK(cond) do { \
    if (cond) { ++g_passed; } \
    else { ++g_failed; std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } \
} while (0)

#define CHECK_EQ(a, b) do { \
    auto _a = (a); auto _b = (b); \
    if (_a == _b) { ++g_passed; } \
    else { ++g_failed; std::fprintf(stderr, "FAIL %s:%d: %s == %s\n", __FILE__, __LINE__, #a, #b); } \
} while (0)

struct Wait {
    std::mutex m;
    std::condition_variable cv;
    bool fired = false;
    void wait() {
        std::unique_lock<std::mutex> lk(m);
        if (!cv.wait_for(lk, std::chrono::seconds(5), [&] { return fired; })) {
            std::fprintf(stderr, "FAIL: TIMEOUT\n");
            std::exit(1);
        }
    }
    void fire() { std::lock_guard<std::mutex> lk(m); fired = true; cv.notify_one(); }
    void reset() { std::lock_guard<std::mutex> lk(m); fired = false; }
};

const std::string kBase = "http://test.local:8000";

} // namespace

int main() {
    using cs::BrowseItem;
    using cs::CatalogApi;
    using cs::ContentItem;
    using cs::ProviderSections;
    using cs::SearchItem;
    using cs::StreamInfo;
    using cs_test::FakeHttpClient;

    // ----- searchAsync -----
    {
        auto fake = std::make_unique<FakeHttpClient>();
        fake->set("/api/search", R"({"results":[{
            "id":"uakino:film-dune","provider":"uakino","type":"movie",
            "title":"Дюна","year":2021,"poster":"https://x/dune.jpg",
            "url":"https://uakino.club/film/dune.html"}]})");
        CatalogApi api(kBase, std::move(fake));
        Wait w;
        std::vector<SearchItem> out;
        std::string err;
        api.searchAsync("Дюна", [&](bool ok, std::vector<SearchItem> r, std::string e) {
            out = std::move(r); err = std::move(e); w.fire();
        });
        w.wait();
        CHECK(err.empty());
        CHECK_EQ(out.size(), 1u);
        if (!out.empty()) {
            CHECK_EQ(out[0].id, std::string("uakino:film-dune"));
            CHECK_EQ(out[0].title, std::string("Дюна"));
            CHECK_EQ(out[0].year, 2021);
            CHECK_EQ(out[0].type, std::string("movie"));
        }
    }

    // ----- contentAsync with translations_level="episode" -----
    {
        auto fake = std::make_unique<FakeHttpClient>();
        fake->set("/api/content", R"({
            "id":"animeon:s1","type":"anime","title":"Наруто",
            "description":"","poster":"",
            "translations_level":"episode",
            "translations":[],
            "seasons":[{
                "number":1,
                "episodes":[
                    {"number":1,"id":"e1","title":"Еп.1","translations":[{"id":"dub","label":"Дубляж"},{"id":"sub","label":"Субтитри"}]},
                    {"number":2,"id":"e2","title":"Еп.2","translations":[]}
                ]
            }]
        })");
        CatalogApi api(kBase, std::move(fake));
        Wait w;
        ContentItem item;
        std::string err;
        api.contentAsync("animeon:s1", [&](bool ok, ContentItem i, std::string e) {
            (void)ok; item = std::move(i); err = std::move(e); w.fire();
        });
        w.wait();
        CHECK(err.empty());
        CHECK_EQ(item.translationsLevel, std::string("episode"));
        CHECK(item.translations.empty()); // content-level translations empty under "episode"
        CHECK_EQ(item.seasons.size(), 1u);
        if (!item.seasons.empty()) {
            CHECK_EQ(item.seasons[0].episodes.size(), 2u);
            if (item.seasons[0].episodes.size() >= 1) {
                CHECK_EQ(item.seasons[0].episodes[0].translations.size(), 2u);
                if (item.seasons[0].episodes[0].translations.size() >= 1) {
                    CHECK_EQ(item.seasons[0].episodes[0].translations[0].first, std::string("dub"));
                    CHECK_EQ(item.seasons[0].episodes[0].translations[0].second, std::string("Дубляж"));
                }
            }
        }
    }

    // ----- contentAsync with translations_level="content" -----
    {
        auto fake = std::make_unique<FakeHttpClient>();
        fake->set("/api/content", R"({
            "id":"uakino:film-x","type":"movie","title":"X","description":"","poster":"",
            "translations_level":"content",
            "translations":[{"id":"uk","label":"Українська"}],
            "seasons":[]
        })");
        CatalogApi api(kBase, std::move(fake));
        Wait w;
        ContentItem item;
        api.contentAsync("uakino:film-x", [&](bool, ContentItem i, std::string) {
            item = std::move(i); w.fire();
        });
        w.wait();
        CHECK_EQ(item.translationsLevel, std::string("content"));
        CHECK_EQ(item.translations.size(), 1u);
        if (!item.translations.empty()) {
            CHECK_EQ(item.translations[0].first, std::string("uk"));
        }
    }

    // ----- streamAsync -----
    {
        auto fake = std::make_unique<FakeHttpClient>();
        fake->set("/api/stream", R"({
            "url":"https://cdn/uakino/dune.m3u8",
            "type":"m3u8",
            "headers":{"Referer":"https://uakino.club/","User-Agent":"x"}
        })");
        CatalogApi api(kBase, std::move(fake));
        Wait w;
        StreamInfo info;
        api.streamAsync("uakino:film-dune", "uk", [&](bool, StreamInfo i, std::string) {
            info = std::move(i); w.fire();
        });
        w.wait();
        CHECK_EQ(info.url, std::string("https://cdn/uakino/dune.m3u8"));
        CHECK_EQ(info.type, std::string("m3u8"));
        CHECK_EQ(info.headers.size(), 2u);
    }

    // ----- sectionsAsync -----
    {
        auto fake = std::make_unique<FakeHttpClient>();
        fake->set("/api/sections", R"([
            {"provider":"uakino","name":"Uakino","sections":[
                {"id":"filmy","title":"Фільми","type":"movie"},
                {"id":"serials","title":"Серіали","type":"series"}
            ]}
        ])");
        CatalogApi api(kBase, std::move(fake));
        Wait w;
        std::vector<ProviderSections> out;
        api.sectionsAsync([&](bool, std::vector<ProviderSections> v, std::string) {
            out = std::move(v); w.fire();
        });
        w.wait();
        CHECK_EQ(out.size(), 1u);
        if (!out.empty()) {
            CHECK_EQ(out[0].provider, std::string("uakino"));
            CHECK_EQ(out[0].sections.size(), 2u);
        }
    }

    // ----- browseAsync -----
    {
        auto fake = std::make_unique<FakeHttpClient>();
        fake->set("/api/browse", R"({
            "provider":"uakino","section":"filmy","page":1,"has_next":true,
            "results":[{"id":"a","provider":"uakino","type":"movie","title":"A","year":2020,"poster":"","url":"u"}]
        })");
        CatalogApi api(kBase, std::move(fake));
        Wait w;
        BrowseItem item;
        api.browseAsync("uakino", "filmy", 1, [&](bool, BrowseItem b, std::string) {
            item = std::move(b); w.fire();
        });
        w.wait();
        CHECK(item.hasNext);
        CHECK_EQ(item.results.size(), 1u);
    }

    // ----- loadPoster -----
    {
        auto fake = std::make_unique<FakeHttpClient>();
        std::vector<std::uint8_t> bytes = {0xFF, 0xD8, 0xFF, 0xE0}; // JPEG SOI
        fake->setBytes("https://cdn/dune.jpg", bytes, "image/jpeg");
        CatalogApi api(kBase, std::move(fake));
        Wait w;
        std::vector<std::uint8_t> got;
        std::string ct;
        api.loadPoster("https://cdn/dune.jpg", [&](bool, std::vector<std::uint8_t> b, std::string c, std::string) {
            got = std::move(b); ct = std::move(c); w.fire();
        });
        w.wait();
        CHECK_EQ(got, bytes);
        CHECK_EQ(ct, std::string("image/jpeg"));
    }

    // ----- Network failure (no route) -----
    {
        auto fake = std::make_unique<FakeHttpClient>(); // empty routes
        CatalogApi api(kBase, std::move(fake));
        Wait w;
        bool ok = true;
        std::string err;
        api.searchAsync("anything", [&](bool o, std::vector<SearchItem>, std::string e) {
            ok = o; err = std::move(e); w.fire();
        });
        w.wait();
        CHECK(!ok);
        CHECK_EQ(err, std::string("error_network"));
    }

    // ----- Concurrency: 10 callbacks fire in order -----
    {
        auto fake = std::make_unique<FakeHttpClient>();
        fake->set("/api/search", R"({"results":[{"id":"x","provider":"uakino","type":"movie","title":"X","year":0,"poster":"","url":"u"}]})");
        CatalogApi api(kBase, std::move(fake));
        std::mutex order_m;
        std::vector<int> order;
        std::atomic<int> done{0};
        std::condition_variable done_cv;
        std::mutex done_m;
        for (int i = 0; i < 10; ++i) {
            api.searchAsync("q" + std::to_string(i), [i, &order_m, &order, &done, &done_cv, &done_m](bool, std::vector<SearchItem>, std::string) {
                {
                    std::lock_guard<std::mutex> lk(order_m);
                    order.push_back(i);
                }
                if (done.fetch_add(1) == 9) {
                    std::lock_guard<std::mutex> lk(done_m);
                    done_cv.notify_one();
                }
            });
        }
        {
            std::unique_lock<std::mutex> lk(done_m);
            done_cv.wait_for(lk, std::chrono::seconds(5), [&] { return done.load() == 10; });
        }
        CHECK_EQ(done.load(), 10);
        // Order is best-effort (worker thread), but all 10 must fire.
        CHECK_EQ(order.size(), 10u);
    }

    // ----- URL contract: /api/content/{id} is a path param, not a query string -----
    // Backend main.py:140 exposes "@app.get('/api/content/{content_id:path}')".
    // The previous C++ builder used "/api/content?id=..." which 404s in
    // production. This test pins the path-param shape via FakeHttpClient.lastUrl.
    // `fake` is std::move()d into CatalogApi; we keep a raw pointer to it
    // for the post-call assertion (raw pointer remains valid until the
    // owning api goes out of scope).
    {
        auto fake_owned = std::make_unique<FakeHttpClient>();
        FakeHttpClient *fake = fake_owned.get();
        fake->set("/api/content", R"({"id":"uakino:film-x","type":"movie","title":"","description":"","poster":"","translations":[],"seasons":[]})");
        CatalogApi api(kBase, std::move(fake_owned));
        Wait w;
        api.contentAsync("uakino:film-x", [&](bool, ContentItem, std::string) { w.fire(); });
        w.wait();
        CHECK_EQ(fake->lastUrl(),
                 std::string(kBase) + "/api/content/uakino%3Afilm-x");
    }

    // ----- URL contract: /api/stream/{id}?translation=... -----
    {
        auto fake_owned = std::make_unique<FakeHttpClient>();
        FakeHttpClient *fake = fake_owned.get();
        fake->set("/api/stream", R"({"url":"https://cdn/x.m3u8","type":"m3u8","headers":{}})");
        CatalogApi api(kBase, std::move(fake_owned));
        Wait w;
        api.streamAsync("uakino:film-x", "uk",
                        [&](bool, StreamInfo, std::string) { w.fire(); });
        w.wait();
        CHECK_EQ(fake->lastUrl(),
                 std::string(kBase) + "/api/stream/uakino%3Afilm-x?translation=uk");
    }

    // ----- URL contract: /api/stream/{id} without translation omits the query -----
    {
        auto fake_owned = std::make_unique<FakeHttpClient>();
        FakeHttpClient *fake = fake_owned.get();
        fake->set("/api/stream", R"({"url":"https://cdn/x.m3u8","type":"m3u8","headers":{}})");
        CatalogApi api(kBase, std::move(fake_owned));
        Wait w;
        api.streamAsync("uakino:film-x", "",
                        [&](bool, StreamInfo, std::string) { w.fire(); });
        w.wait();
        CHECK_EQ(fake->lastUrl(),
                 std::string(kBase) + "/api/stream/uakino%3Afilm-x");
    }

    // ----- Destruction safety: post a job then destruct immediately -----
    {
        auto fake = std::make_unique<FakeHttpClient>();
        fake->set("/api/search", R"({"results":[]})");
        bool destroyed = false;
        {
            CatalogApi api(kBase, std::move(fake));
            api.searchAsync("x", [&destroyed](bool, std::vector<SearchItem>, std::string) {
                destroyed = true;
            });
        } // api destructs here; worker thread joins cleanly
        // The destructor blocks until the worker has processed queued jobs,
        // so `destroyed` must be true after.
        CHECK(destroyed);
    }

    std::fprintf(stderr, "\n%d passed, %d failed\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
