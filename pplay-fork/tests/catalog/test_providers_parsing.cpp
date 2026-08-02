// Tests for parseProviders() (issue #73).
//
// The backend's /api/providers response is a bare JSON array of
// {id, name, status, last_error_at} objects. The parser must:
//   * extract the array (and accept {"providers": [...]} for symmetry
//     with parseSections),
//   * default an unknown status to "ok" so the chip strip treats
//     unrecognized providers as enabled (defensive against backend
//     drift),
//   * drop rows with no id (defensive against malformed responses).

#include <cstdio>
#include <string>

#include "CatalogApi.h"

namespace {

int g_passed = 0;
int g_failed = 0;

// String/equality comparison. Uses __LINE__-suffixed names so multiple
// CHECK_EQ calls in the same scope don't shadow each other (the original
// pair-test approach used `_a`/`_b` which collided when called twice in
// one function).
#define CHECK_EQ(a, b) do { \
    auto _check_eq_a_##__LINE__ = (a); \
    auto _check_eq_b_##__LINE__ = (b); \
    if (_check_eq_a_##__LINE__ == _check_eq_b_##__LINE__) { ++g_passed; } \
    else { ++g_failed; std::fprintf(stderr, "FAIL %s:%d: %s == %s\n", \
        __FILE__, __LINE__, #a, #b); } \
} while (0)

// Integer-specific comparison with formatted output (got %lld vs %lld).
// Casts via the C-style `(long long)` syntax — `long long(x)` is not a
// valid function-style cast because `long long` is two type tokens.
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

    // Bare array form (the actual /api/providers response).
    {
        const char *raw = R"([
            {"id": "uakino", "name": "Uakino",        "status": "ok",       "last_error_at": 0},
            {"id": "toloka", "name": "Toloka",        "status": "degraded", "last_error_at": 1700000000},
            {"id": "broken", "name": "Broken Source", "status": "down",    "last_error_at": 1700001234}
        ])";
        auto parsed = CatalogApi::parseProviders(raw);
        CHECK_SIZE(parsed.size(), 3);
        CHECK_EQ(parsed[0].id, std::string("uakino"));
        CHECK_EQ(parsed[0].status, std::string("ok"));
        CHECK_EQ_INT(parsed[0].lastErrorAt, 0);
        CHECK_EQ(parsed[1].id, std::string("toloka"));
        CHECK_EQ(parsed[1].status, std::string("degraded"));
        CHECK_EQ_INT(parsed[1].lastErrorAt, 1700000000);
        CHECK_EQ(parsed[2].id, std::string("broken"));
        CHECK_EQ(parsed[2].status, std::string("down"));
    }

    // Wrapped form (symmetry with parseSections).
    {
        const char *raw = R"({"providers":[
            {"id": "uakino", "name": "Uakino", "status": "ok"}
        ]})";
        auto parsed = CatalogApi::parseProviders(raw);
        CHECK_SIZE(parsed.size(), 1);
        CHECK_EQ(parsed[0].id, std::string("uakino"));
        CHECK_EQ(parsed[0].status, std::string("ok"));
    }

    // Unknown status string collapses to "ok" (defensive).
    {
        const char *raw = R"([{"id": "x", "name": "X", "status": "weird"}])";
        auto parsed = CatalogApi::parseProviders(raw);
        CHECK_SIZE(parsed.size(), 1);
        CHECK_EQ(parsed[0].status, std::string("ok"));
    }

    // Empty id is dropped — no zombie entries in the chip strip.
    {
        const char *raw = R"([
            {"id": "", "name": "Empty", "status": "ok"},
            {"id": "uakino", "name": "Uakino", "status": "ok"}
        ])";
        auto parsed = CatalogApi::parseProviders(raw);
        CHECK_SIZE(parsed.size(), 1);
        CHECK_EQ(parsed[0].id, std::string("uakino"));
    }

    // Missing last_error_at defaults to 0.
    {
        const char *raw = R"([{"id": "uakino", "name": "Uakino", "status": "ok"}])";
        auto parsed = CatalogApi::parseProviders(raw);
        CHECK_SIZE(parsed.size(), 1);
        CHECK_EQ_INT(parsed[0].lastErrorAt, 0);
    }

    std::printf("providers_parsing: %d passed, %d failed\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
