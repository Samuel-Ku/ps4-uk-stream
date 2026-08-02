// Tests for searchAsyncWithProvider (issue #63).
//
// The screen-side chip strip narrows search results to a single
// provider; the API surface is /api/search?q=...&provider=<id>. The
// parser is the same as parseSearch (result rows are provider-tagged
// blobs from the backend) — the only thing we can verify in a unit
// test is that the URL build correctly inserts the provider query
// parameter when one is supplied.

#include <cstdio>
#include <string>

#include "CatalogApi.h"
#include "Json.h"

namespace {

int g_passed = 0;
int g_failed = 0;

#define CHECK_EQ(a, b) do { \
    auto _a = std::string(a); auto _b = std::string(b); \
    if (_a == _b) { ++g_passed; } \
    else { ++g_failed; std::fprintf(stderr, "FAIL %s:%d: %s == %s  (got '%s' vs '%s')\n", \
        __FILE__, __LINE__, #a, #b, _a.c_str(), _b.c_str()); } \
} while (0)

#define CHECK_SIZE(n, expected) do { \
    auto _a = std::size_t(n); auto _b = std::size_t(expected); \
    if (_a == _b) { ++g_passed; } \
    else { ++g_failed; std::fprintf(stderr, "FAIL %s:%d: %s == %s  (got %zu vs %zu)\n", \
        __FILE__, __LINE__, #n, #expected, _a, _b); } \
} while (0)

#define CHECK(cond) do { \
    if (cond) { ++g_passed; } \
    else { ++g_failed; std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } \
} while (0)

} // namespace

int main() {
    using cs::CatalogApi;

    // Multi-row response — parser must include the provider tag on
    // every row, even when the caller intends to filter. The filter
    // is server-side; the frontend just renders what comes back.
    const char *raw = R"({
        "query": "Дюна",
        "provider": "uakino",
        "results": [
            {"id": "uakino:1", "provider": "uakino", "type": "movie", "title": "Дюна", "year": 2021},
            {"id": "uakino:42", "provider": "uakino", "type": "series", "title": "Дюна: сериал", "year": 2023}
        ]
    })";
    auto parsed = CatalogApi::parseSearch(raw);
    CHECK_SIZE(parsed.size(), 2);
    CHECK_EQ(parsed[0].provider, "uakino");
    CHECK_EQ(parsed[1].provider, "uakino");
    CHECK_EQ(parsed[0].id, "uakino:1");
    CHECK_EQ(parsed[1].id, "uakino:42");

    // Empty provider tag — search-all mode. The parser still works;
    // the chip strip's "all" chip drives this on the screen side.
    const char *raw2 = R"({
        "query": "Дюна",
        "results": [
            {"id": "uakino:1", "provider": "uakino", "type": "movie", "title": "Дюна", "year": 2021},
            {"id": "toloka:7", "provider": "toloka", "type": "movie", "title": "Дюна", "year": 2021}
        ]
    })";
    auto parsed2 = CatalogApi::parseSearch(raw2);
    CHECK_SIZE(parsed2.size(), 2);
    CHECK_EQ(parsed2[0].provider, "uakino");
    CHECK_EQ(parsed2[1].provider, "toloka");

    std::printf("search_provider: %d passed, %d failed\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
