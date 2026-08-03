// Tests for parseHome() (issue #61).
//
// The backend's /api/home response is {"rows": [...]}, where each row
// has {title, type, items: [HomeItem...]}. The parser must:
//   * extract rows + items, preserving order;
//   * drop items with empty group_key (defensive — round-tripping
//     these through /api/content/{gk} would 404);
//   * drop empty rows (defensive — backend already filters these);
//   * tolerate missing optional fields (poster, member_keys).

#include <cstdio>
#include <string>

#include "CatalogApi.h"

namespace {

int g_passed = 0;
int g_failed = 0;

#define CHECK_EQ(a, b) do { \
    auto _check_eq_a_##__LINE__ = (a); \
    auto _check_eq_b_##__LINE__ = (b); \
    if (_check_eq_a_##__LINE__ == _check_eq_b_##__LINE__) { ++g_passed; } \
    else { ++g_failed; std::fprintf(stderr, "FAIL %s:%d: %s == %s\n", \
        __FILE__, __LINE__, #a, #b); } \
} while (0)

#define CHECK_EQ_INT(a, b) do { \
    auto _check_eqi_a_##__LINE__ = (long long)(a); \
    auto _check_eqi_b_##__LINE__ = (long long)(b); \
    if (_check_eqi_a_##__LINE__ == _check_eqi_b_##__LINE__) { ++g_passed; } \
    else { ++g_failed; std::fprintf(stderr, "FAIL %s:%d: %s == %s  (got %lld vs %lld)\n", \
        __FILE__, __LINE__, #a, #b, _check_eqi_a_##__LINE__, _check_eqi_b_##__LINE__); } \
} while (0)

#define CHECK_SIZE(n, expected) do { \
    auto _check_size_a_##__LINE__ = std::size_t(n); \
    auto _check_size_b_##__LINE__ = std::size_t(expected); \
    if (_check_size_a_##__LINE__ == _check_size_b_##__LINE__) { ++g_passed; } \
    else { ++g_failed; std::fprintf(stderr, "FAIL %s:%d: %s == %s  (got %zu vs %zu)\n", \
        __FILE__, __LINE__, #n, #expected, _check_size_a_##__LINE__, _check_size_b_##__LINE__); } \
} while (0)

#define CHECK(cond) do { \
    if (cond) { ++g_passed; } \
    else { ++g_failed; std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } \
} while (0)

} // namespace

int main() {
    using cs::CatalogApi;

    // Two-row response: «Новинки» + «Фільми». Verifies field mapping
    // and order preservation.
    {
        const char *raw = R"({
            "rows": [
                {
                    "title": "Новинки",
                    "type": "newest",
                    "items": [
                        {
                            "group_key": "g1:title:дюна:movie:2021",
                            "title": "Дюна",
                            "year": 2021,
                            "type": "movie",
                            "poster": "https://x/dune.jpg",
                            "providers": ["uakino", "toloka"],
                            "member_keys": [
                                "g1:title:дюна:movie:2021",
                                "g1:title:дюна:movie:0"
                            ]
                        },
                        {
                            "group_key": "g1:title:опіум:movie:2023",
                            "title": "Опіум",
                            "year": 2023,
                            "type": "movie",
                            "poster": "https://x/opium.jpg",
                            "providers": ["uakino"],
                            "member_keys": ["g1:title:опіум:movie:2023"]
                        }
                    ]
                },
                {
                    "title": "Фільми",
                    "type": "movie",
                    "items": [
                        {
                            "group_key": "g1:title:інтерстеллар:movie:2014",
                            "title": "Інтерстеллар",
                            "year": 2014,
                            "type": "movie",
                            "poster": "https://x/inter.jpg",
                            "providers": ["toloka"],
                            "member_keys": ["g1:title:інтерстеллар:movie:2014"]
                        }
                    ]
                }
            ]
        })";
        auto parsed = CatalogApi::parseHome(raw);
        CHECK_SIZE(parsed.rows.size(), 2);
        CHECK_EQ(parsed.rows[0].title, std::string("Новинки"));
        CHECK_EQ(parsed.rows[0].type, std::string("newest"));
        CHECK_SIZE(parsed.rows[0].items.size(), 2);
        CHECK_EQ(parsed.rows[0].items[0].groupKey,
                 std::string("g1:title:дюна:movie:2021"));
        CHECK_EQ(parsed.rows[0].items[0].title, std::string("Дюна"));
        CHECK_EQ_INT(parsed.rows[0].items[0].year, 2021);
        CHECK_EQ(parsed.rows[0].items[0].type, std::string("movie"));
        CHECK_EQ(parsed.rows[0].items[0].poster, std::string("https://x/dune.jpg"));
        CHECK_SIZE(parsed.rows[0].items[0].providers.size(), 2);
        CHECK_EQ(parsed.rows[0].items[0].providers[0], std::string("uakino"));
        CHECK_EQ(parsed.rows[0].items[0].providers[1], std::string("toloka"));
        CHECK_SIZE(parsed.rows[0].items[0].memberKeys.size(), 2);
        CHECK_EQ(parsed.rows[0].items[1].year, 2023);
        CHECK_EQ(parsed.rows[1].title, std::string("Фільми"));
        CHECK_EQ(parsed.rows[1].type, std::string("movie"));
        CHECK_SIZE(parsed.rows[1].items.size(), 1);
    }

    // Conditional rows: only «Новинки» + «Серіали». «Популярні зараз»
    // absent — server filters empty rows; we don't synthesize anything.
    {
        const char *raw = R"({
            "rows": [
                {"title": "Новинки", "type": "newest", "items": []},
                {"title": "Серіали", "type": "series", "items": [
                    {"group_key": "gk", "title": "T", "year": 2024, "type": "series",
                     "poster": "", "providers": ["uakino"], "member_keys": ["gk"]}
                ]}
            ]
        })";
        auto parsed = CatalogApi::parseHome(raw);
        // The empty «Новинки» row is dropped — defensive against backend
        // drift. The user sees only «Серіали».
        CHECK_SIZE(parsed.rows.size(), 1);
        CHECK_EQ(parsed.rows[0].title, std::string("Серіали"));
        CHECK_EQ(parsed.rows[0].type, std::string("series"));
    }

    // Item with empty group_key is dropped (resume anchor gone).
    {
        const char *raw = R"({
            "rows": [{
                "title": "Новинки", "type": "newest",
                "items": [
                    {"group_key": "", "title": "Ghost", "year": 2024, "type": "movie",
                     "poster": "", "providers": ["uakino"], "member_keys": []},
                    {"group_key": "real-gk", "title": "Real", "year": 2024, "type": "movie",
                     "poster": "", "providers": ["uakino"], "member_keys": ["real-gk"]}
                ]
            }]
        })";
        auto parsed = CatalogApi::parseHome(raw);
        CHECK_SIZE(parsed.rows.size(), 1);
        CHECK_SIZE(parsed.rows[0].items.size(), 1);
        CHECK_EQ(parsed.rows[0].items[0].groupKey, std::string("real-gk"));
    }

    // Missing optional fields: poster, member_keys default cleanly.
    {
        const char *raw = R"({
            "rows": [{
                "title": "Новинки", "type": "newest",
                "items": [
                    {"group_key": "k", "title": "Minimal", "year": 0, "type": "movie",
                     "providers": ["uakino"]}
                ]
            }]
        })";
        auto parsed = CatalogApi::parseHome(raw);
        CHECK_SIZE(parsed.rows.size(), 1);
        CHECK_SIZE(parsed.rows[0].items.size(), 1);
        CHECK_EQ(parsed.rows[0].items[0].title, std::string("Minimal"));
        CHECK_EQ_INT(parsed.rows[0].items[0].year, 0);
        CHECK_EQ(parsed.rows[0].items[0].poster, std::string(""));
        CHECK_SIZE(parsed.rows[0].items[0].memberKeys.size(), 0);
    }

    // Empty / malformed payloads return an empty response — no throw.
    {
        auto empty = CatalogApi::parseHome("");
        CHECK_SIZE(empty.rows.size(), 0);
        auto malformed = CatalogApi::parseHome("not json at all");
        CHECK_SIZE(malformed.rows.size(), 0);
        auto noRows = CatalogApi::parseHome("{}");
        CHECK_SIZE(noRows.rows.size(), 0);
    }

    std::printf("home_parsing: %d passed, %d failed\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
