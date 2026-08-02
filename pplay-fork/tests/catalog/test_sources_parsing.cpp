// Tests for parseContent()'s sources-array handling (issue #62 / v3 spec §3.3).
//
// The backend returns a `sources` array on /api/content responses when
// the group is served by multiple providers. Each entry has {provider,
// id} so the chip strip can refetch the same group under a different
// provider. The parser must:
//   * extract the array when present
//   * drop entries with empty fields
//   * synthesize a single-entry roster from `id` when the field is
//     missing (legacy / single-source / test fixtures)

#include <cstdio>
#include <string>

#include "CatalogApi.h"

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

    // Multi-source response: two providers, group_key present.
    {
        const char *raw = R"({
            "id": "uakino:42",
            "type": "series",
            "title": "Dune",
            "group_key": "g1:dune",
            "sources": [
                {"provider": "uakino", "id": "uakino:42"},
                {"provider": "toloka", "id": "toloka:7"}
            ]
        })";
        auto item = CatalogApi::parseContent(raw);
        CHECK_EQ(item.id, "uakino:42");
        CHECK_EQ(item.groupKey, "g1:dune");
        CHECK_SIZE(item.sources.size(), 2);
        CHECK_EQ(item.sources[0].provider, "uakino");
        CHECK_EQ(item.sources[0].id, "uakino:42");
        CHECK_EQ(item.sources[1].provider, "toloka");
        CHECK_EQ(item.sources[1].id, "toloka:7");
    }

    // Empty sources array → fall back to synthesizing from id.
    {
        const char *raw = R"({
            "id": "uakino:42",
            "type": "movie",
            "title": "Dune",
            "sources": []
        })";
        auto item = CatalogApi::parseContent(raw);
        CHECK_SIZE(item.sources.size(), 1);
        CHECK_EQ(item.sources[0].provider, "uakino");
        CHECK_EQ(item.sources[0].id, "uakino:42");
    }

    // Missing sources field entirely → also synthesize from id.
    {
        const char *raw = R"({
            "id": "toloka:7",
            "type": "movie",
            "title": "Dune"
        })";
        auto item = CatalogApi::parseContent(raw);
        CHECK_SIZE(item.sources.size(), 1);
        CHECK_EQ(item.sources[0].provider, "toloka");
        CHECK_EQ(item.sources[0].id, "toloka:7");
    }

    // Id without ":" separator → entire id is treated as the provider.
    {
        const char *raw = R"({"id": "bare", "type": "movie", "title": "X"})";
        auto item = CatalogApi::parseContent(raw);
        CHECK_SIZE(item.sources.size(), 1);
        CHECK_EQ(item.sources[0].provider, "bare");
        CHECK_EQ(item.sources[0].id, "bare");
    }

    // Entries with empty fields are dropped (defensive — malformed
    // responses must not crash the strip).
    {
        const char *raw = R"({
            "id": "uakino:1",
            "type": "movie",
            "title": "X",
            "sources": [
                {"provider": "", "id": "uakino:1"},
                {"provider": "uakino", "id": ""},
                {"provider": "uakino", "id": "uakino:1"}
            ]
        })";
        auto item = CatalogApi::parseContent(raw);
        // The two malformed entries are dropped; one valid entry remains.
        CHECK_SIZE(item.sources.size(), 1);
        CHECK_EQ(item.sources[0].provider, "uakino");
        CHECK_EQ(item.sources[0].id, "uakino:1");
    }

    // Empty `id` at the top level (no synthesis possible) → empty
    // sources vector. The screen refuses to render an empty strip.
    {
        const char *raw = R"({"id": "", "type": "movie", "title": "X"})";
        auto item = CatalogApi::parseContent(raw);
        CHECK_SIZE(item.sources.size(), 0);
    }

    std::printf("sources_parsing: %d passed, %d failed\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
