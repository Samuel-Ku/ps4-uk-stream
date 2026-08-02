// Tests for source/dub memory pre-focus (issue #67).
//
// The CatalogState store keeps MemoryEntry records keyed by groupKey.
// The store is the canonical authority — ScreenContent reads from it
// to pre-focus the chip strip + translation on a second open. The
// selection logic lives in ScreenContent (applyMemoryPreFocus), but
// the store-side rules tested here are what the screen reads:
//
//   * setMemory / memory round-trip a record by groupKey.
//   * The LRU cap holds at kMaxEntries.
//   * LRU evicts the OLDEST updatedAt, not random.
//   * Multiple records for distinct groupKeys coexist.

#include <cstdio>
#include <string>

#include "CatalogState.h"

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
    using cs::CatalogState;
    using cs::MemoryEntry;

    // Round-trip: setMemory / memory returns the same provider+label.
    {
        CatalogState st("/tmp/catalog_state_test_memory_pf_1.json");
        MemoryEntry e;
        e.groupKey = "g1:title:one-piece:series:1999";
        e.provider = "uakino";
        e.translationLabel = "Українська";
        e.updatedAt = 1000;
        st.setMemory(e);
        const MemoryEntry *got = st.memory(e.groupKey);
        CHECK(got != nullptr);
        if (got) {
            CHECK_EQ(got->provider, std::string("uakino"));
            CHECK_EQ(got->translationLabel, std::string("Українська"));
            CHECK_EQ_INT(got->updatedAt, 1000);
        }
        // Missing key returns nullptr.
        CHECK(st.memory("g1:title:missing:series:0") == nullptr);
    }

    // Upsert: a second setMemory for the same groupKey replaces the old
    // record (and the count stays at 1, not 2).
    {
        CatalogState st("/tmp/catalog_state_test_memory_pf_2.json");
        MemoryEntry a;
        a.groupKey = "gk";
        a.provider = "uakino";
        a.translationLabel = "Українська";
        a.updatedAt = 1000;
        st.setMemory(a);
        MemoryEntry b;
        b.groupKey = "gk";
        b.provider = "toloka";
        b.translationLabel = "Субтитри";
        b.updatedAt = 2000;
        st.setMemory(b);
        const MemoryEntry *got = st.memory("gk");
        CHECK(got != nullptr);
        if (got) {
            CHECK_EQ(got->provider, std::string("toloka"));
            CHECK_EQ(got->translationLabel, std::string("Субтитри"));
            CHECK_EQ_INT(got->updatedAt, 2000);
        }
        // recentResume / memory iteration isn't exposed for MemoryEntry
        // (only for resume), but the size cap is enforced by setMemory
        // through the same LRU trim path — verified in the LRU test.
    }

    // LRU cap: insert kMaxEntries + 5 records, oldest 5 by updatedAt
    // get evicted, freshest 50 stay.
    {
        CatalogState st("/tmp/catalog_state_test_memory_pf_3.json");
        for (int i = 0; i < static_cast<int>(CatalogState::kMaxEntries) + 5; ++i) {
            MemoryEntry e;
            e.groupKey = "gk-" + std::to_string(i);
            e.provider = "uakino";
            e.translationLabel = "L";
            e.updatedAt = static_cast<std::int64_t>(i);
            st.setMemory(e);
        }
        // The first 5 (gk-0..gk-4, the oldest updatedAt) are evicted;
        // gk-5..gk-54 stay.
        CHECK(st.memory("gk-0") == nullptr);
        CHECK(st.memory("gk-4") == nullptr);
        CHECK(st.memory("gk-5") != nullptr);
        CHECK(st.memory("gk-54") != nullptr);
    }

    // Atomic save / load round-trip — confirms a memory record survives
    // process restart (the user closes the app and reopens).
    {
        const std::string path = "/tmp/catalog_state_test_memory_pf_4.json";
        CatalogState st(path);
        MemoryEntry e;
        e.groupKey = "gk-save";
        e.provider = "uakino";
        e.translationLabel = "Українська";
        e.updatedAt = 9999;
        st.setMemory(e);
        CHECK(st.save());
        // Reload from disk.
        CatalogState st2(path);
        CHECK(st2.load());
        const MemoryEntry *got = st2.memory("gk-save");
        CHECK(got != nullptr);
        if (got) {
            CHECK_EQ(got->provider, std::string("uakino"));
            CHECK_EQ(got->translationLabel, std::string("Українська"));
            CHECK_EQ_INT(got->updatedAt, 9999);
        }
    }

    // Movies store nothing — issue #67 policy lives at the call site
    // (shouldRememberMemory in CatalogApi.h) and never inserts a
    // MemoryEntry for items with empty seasons. We confirm by
    // reproducing the policy: if a caller erroneously inserts a movie
    // record, the store still returns it (the store is dumb). The
    // screen-side guard is what keeps movies out.
    {
        CatalogState st("/tmp/catalog_state_test_memory_pf_5.json");
        // Movie record: empty groupKey would 404, but the store
        // doesn't reject it. Verify the store's contract is
        // "transparent — what you set is what you get".
        MemoryEntry movie;
        movie.groupKey = "g1:title:oppenheimer:movie:2023";
        movie.provider = "uakino";
        movie.translationLabel = "Українська";
        movie.updatedAt = 1;
        st.setMemory(movie);
        CHECK(st.memory(movie.groupKey) != nullptr);
    }

    std::printf("memory_prefocus: %d passed, %d failed\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
