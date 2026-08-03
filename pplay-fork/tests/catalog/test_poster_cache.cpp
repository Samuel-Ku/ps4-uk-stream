// Tests for the poster disk cache (issue #54, v3 spec §8).
//
// Seams under test:
//   - cs::DiskPosterCache (get/put, TTL, extension<->content-type mapping)
//   - CatalogApi::loadPoster with setPosterCacheDir (fetch-once-then-disk,
//     cold start served from disk)

#include "../standalone-catalog/FakeHttpClient.h"

#include "CatalogApi.h"
#include "PosterCache.h"

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace fs = std::filesystem;

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

fs::path freshTmpDir(const std::string &tag) {
    fs::path dir = fs::temp_directory_path() / ("pplay_poster_test_" + tag + "_" +
                   std::to_string(static_cast<long long>(
                       std::chrono::steady_clock::now().time_since_epoch().count())));
    fs::remove_all(dir);
    return dir;
}

const std::vector<std::uint8_t> kBytesA = {0xff, 0xd8, 0xff, 0xe0, 1, 2, 3};
const std::vector<std::uint8_t> kBytesB = {0x89, 0x50, 0x4e, 0x47, 4, 5, 6};
const std::string kUrl = "http://backend.local:8000/api/poster?u=http%3A%2F%2Fimg.example%2Fa.jpg";

} // namespace

int main() {
    // ---------------- DiskPosterCache (unit seam) -------------------------
    {
        fs::path dir = freshTmpDir("unit");
        cs::DiskPosterCache cache(dir.string());

        std::vector<std::uint8_t> out;
        std::string ct;
        CHECK(!cache.get(kUrl, out, ct));  // miss on empty cache

        cache.put(kUrl, kBytesA, "image/jpeg");
        CHECK(cache.get(kUrl, out, ct));
        CHECK_EQ(out.size(), kBytesA.size());
        CHECK_EQ(ct, std::string("image/jpeg"));

        // Overwrite: later put wins.
        cache.put(kUrl, kBytesB, "image/jpeg");
        CHECK(cache.get(kUrl, out, ct));
        CHECK_EQ(out.size(), kBytesB.size());

        // PNG keeps its content type through the extension mapping.
        cache.put(kUrl, kBytesB, "image/png");
        CHECK(cache.get(kUrl, out, ct));
        CHECK_EQ(ct, std::string("image/png"));

        // Non-image types normalize to jpeg (backend contract).
        cache.put(kUrl, kBytesA, "text/plain");
        CHECK(cache.get(kUrl, out, ct));
        CHECK_EQ(ct, std::string("image/jpeg"));

        // Expired entries miss: backdate everything past the 7-day TTL.
        for (const auto &e : fs::directory_iterator(dir)) {
            fs::last_write_time(e.path(),
                fs::file_time_type::clock::now() - std::chrono::hours(24 * 8));
        }
        CHECK(!cache.get(kUrl, out, ct));

        // Unusable root (a regular file, not a dir) never throws, never serves.
        fs::path fileRoot = dir / "not_a_dir";
        { std::ofstream f(fileRoot); f << "x"; }
        cs::DiskPosterCache broken(fileRoot.string());
        broken.put(kUrl, kBytesA, "image/jpeg");  // must not crash
        CHECK(!broken.get(kUrl, out, ct));

        fs::remove_all(dir);
    }

    // ---------------- CatalogApi + disk cache (integration seam) ----------
    {
        fs::path dir = freshTmpDir("api");

        auto fake = std::make_unique<cs_test::FakeHttpClient>();
        fake->setBytes("img.example", kBytesA, "image/jpeg");
        cs_test::FakeHttpClient *fakePtr = fake.get();

        {
            cs::CatalogApi api("http://backend.local:8000", std::move(fake));
            api.setPosterCacheDir(dir.string());

            Wait w;
            bool ok = false;
            std::vector<std::uint8_t> bytes;
            std::string ct;
            api.loadPoster(kUrl, [&](bool o, std::vector<std::uint8_t> b, std::string c, std::string) {
                ok = o; bytes = std::move(b); ct = std::move(c); w.fire();
            });
            w.wait();
            CHECK(ok);
            CHECK_EQ(bytes.size(), kBytesA.size());
            CHECK_EQ(ct, std::string("image/jpeg"));
            CHECK_EQ(fakePtr->getBytesCount(), 1);

            // Second call: served from disk, no additional fetch.
            w.reset(); ok = false;
            api.loadPoster(kUrl, [&](bool o, std::vector<std::uint8_t> b, std::string c, std::string) {
                ok = o; bytes = std::move(b); ct = std::move(c); w.fire();
            });
            w.wait();
            CHECK(ok);
            CHECK_EQ(bytes.size(), kBytesA.size());
            CHECK_EQ(fakePtr->getBytesCount(), 1);
        } // api destroyed — worker joined, cache files on disk

        // Cold start: brand-new CatalogApi on the same dir, fresh fake.
        auto fake2 = std::make_unique<cs_test::FakeHttpClient>();
        fake2->setBytes("img.example", kBytesB, "image/jpeg");  // would return DIFFERENT bytes
        cs_test::FakeHttpClient *fake2Ptr = fake2.get();
        {
            cs::CatalogApi api("http://backend.local:8000", std::move(fake2));
            api.setPosterCacheDir(dir.string());

            Wait w;
            bool ok = false;
            std::vector<std::uint8_t> bytes;
            api.loadPoster(kUrl, [&](bool o, std::vector<std::uint8_t> b, std::string, std::string) {
                ok = o; bytes = std::move(b); w.fire();
            });
            w.wait();
            CHECK(ok);
            CHECK_EQ(bytes.size(), kBytesA.size());   // cached bytes, not the new fake's
            CHECK_EQ(fake2Ptr->getBytesCount(), 0);   // no fetch at all
        }

        fs::remove_all(dir);
    }

    // ---------------- No cache dir: legacy always-fetch behaviour ---------
    {
        auto fake = std::make_unique<cs_test::FakeHttpClient>();
        fake->setBytes("img.example", kBytesA, "image/jpeg");
        cs_test::FakeHttpClient *fakePtr = fake.get();
        cs::CatalogApi api("http://backend.local:8000", std::move(fake));
        // setPosterCacheDir NOT called.

        for (int i = 0; i < 2; ++i) {
            Wait w;
            bool ok = false;
            api.loadPoster(kUrl, [&](bool o, std::vector<std::uint8_t>, std::string, std::string) {
                ok = o; w.fire();
            });
            w.wait();
            CHECK(ok);
        }
        CHECK_EQ(fakePtr->getBytesCount(), 2);  // fetched both times
    }

    std::printf("poster_cache: %d passed, %d failed\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
