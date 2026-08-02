// Tests for the «Продовжити перегляд» row on Home (issue #66).
//
// ScreenHome::prependResumeRow() reads up to 20 most-recent resume
// entries from the CatalogState store, drops finished ones (>= 95%
// per CatalogState::isFinished), and synthesizes a row. The store-side
// rules tested here are what the screen reads:
//
//   * recentResume returns entries in updatedAt-descending order.
//   * recentResume respects the limit (≤ 20).
//   * isFinished drops entries at >= 95% of a known duration.
//   * isFinished keeps entries with unknown duration (warning, not
//     re-offer — the row shouldn't disappear on a malformed record).
//   * setResume upserts by groupKey — same group re-recorded moves
//     to the top of the recent list.

#include <cstdio>
#include <string>
#include <vector>

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

cs::ResumeEntry makeEntry(const std::string &gk, const std::string &prov,
                          long pos, long dur, std::int64_t when) {
    cs::ResumeEntry e;
    e.groupKey = gk;
    e.provider = prov;
    e.id = "id-" + gk;
    e.episodeId = (prov == "uakino-movies") ? std::string() : "ep1";
    e.translationLabel = "Українська";
    e.positionSec = pos;
    e.durationSec = dur;
    e.updatedAt = when;
    return e;
}

} // namespace

int main() {
    using cs::CatalogState;
    using cs::ResumeEntry;

    // recentResume orders by updatedAt-descending and respects the limit.
    {
        CatalogState st("/tmp/catalog_state_test_resume_1.json");
        for (int i = 0; i < 5; ++i) {
            st.setResume(makeEntry("gk-" + std::to_string(i), "uakino",
                                    60, 600, static_cast<std::int64_t>(i)));
        }
        const auto live = st.recentResume(20);
        // 5 entries, newest first.
        CHECK_SIZE(live.size(), 5);
        CHECK_EQ(live.front().groupKey, std::string("gk-4"));
        CHECK_EQ(live.back().groupKey, std::string("gk-0"));
    }

    // recentResume respects the limit (≤ 20).
    {
        CatalogState st("/tmp/catalog_state_test_resume_2.json");
        for (int i = 0; i < 25; ++i) {
            st.setResume(makeEntry("gk-x" + std::to_string(i), "uakino",
                                    30, 600, static_cast<std::int64_t>(i)));
        }
        const auto live = st.recentResume(20);
        CHECK_SIZE(live.size(), 20);
        // The 24-th write should be the freshest — index 24.
        CHECK_EQ(live.front().groupKey, std::string("gk-x24"));
    }

    // isFinished: 95% boundary. Spec says "do not re-offer a position
    // once finished". 95% exactly is finished (>=).
    {
        ResumeEntry e;
        e.durationSec = 1000;
        e.positionSec = 950;     // exactly 95%
        CHECK(CatalogState::isFinished(e));
        e.positionSec = 949;     // 94.9% — not finished
        CHECK(!CatalogState::isFinished(e));
        e.positionSec = 0;       // 0% — not finished
        CHECK(!CatalogState::isFinished(e));
    }

    // isFinished with unknown duration (durationSec == 0) is NOT
    // finished — we don't know the total, so we cannot re-offer. This
    // matches the screen's "never punish the user for a missing
    // duration": the row will display the resume position normally.
    {
        ResumeEntry e;
        e.durationSec = 0;
        e.positionSec = 999999;
        CHECK(!CatalogState::isFinished(e));
    }

    // Upsert: re-recording the same groupKey moves it to the top.
    {
        CatalogState st("/tmp/catalog_state_test_resume_3.json");
        st.setResume(makeEntry("gk-A", "uakino", 60, 600, 100));
        st.setResume(makeEntry("gk-B", "uakino", 60, 600, 200));
        // B is newest.
        auto before = st.recentResume(20);
        CHECK_EQ(before.front().groupKey, std::string("gk-B"));
        // Re-record A: now A should be at the top.
        st.setResume(makeEntry("gk-A", "uakino", 90, 600, 300));
        auto after = st.recentResume(20);
        CHECK_EQ(after.front().groupKey, std::string("gk-A"));
        CHECK_SIZE(after.size(), 2);
        // Position got updated.
        const ResumeEntry *pa = st.resume("gk-A");
        CHECK(pa != nullptr);
        if (pa) CHECK_EQ_INT(pa->positionSec, 90);
    }

    // The screen's filter behavior: live resume row drops finished
    // entries and preserves unknowns. Reproduce the filter outside
    // the screen so we can lock the policy.
    {
        CatalogState st("/tmp/catalog_state_test_resume_4.json");
        st.setResume(makeEntry("gk-finished", "uakino",  999, 1000, 10)); // 99.9%
        st.setResume(makeEntry("gk-live",      "uakino",  100, 1000, 20)); // 10%
        st.setResume(makeEntry("gk-unknown",   "uakino",  100,    0, 30)); // dur=0
        st.setResume(makeEntry("gk-edge",      "uakino",  949, 1000, 40)); // 94.9%
        auto recent = st.recentResume(20);
        std::vector<ResumeEntry> live;
        live.reserve(recent.size());
        for (const auto &e : recent) {
            if (!CatalogState::isFinished(e)) live.push_back(e);
        }
        // 3 live, 1 finished (gk-finished).
        CHECK_SIZE(live.size(), 3);
        // Order is updatedAt-descending; gk-edge > gk-unknown > gk-live.
        CHECK_EQ(live[0].groupKey, std::string("gk-edge"));
        CHECK_EQ(live[1].groupKey, std::string("gk-unknown"));
        CHECK_EQ(live[2].groupKey, std::string("gk-live"));
    }

    // Atomic save / load round-trip — confirms a resume survives
    // process restart.
    {
        const std::string path = "/tmp/catalog_state_test_resume_5.json";
        CatalogState st(path);
        st.setResume(makeEntry("gk-save", "uakino", 30, 600, 7));
        CHECK(st.save());
        CatalogState st2(path);
        CHECK(st2.load());
        const ResumeEntry *e = st2.resume("gk-save");
        CHECK(e != nullptr);
        if (e) {
            CHECK_EQ(e->groupKey, std::string("gk-save"));
            CHECK_EQ_INT(e->positionSec, 30);
            CHECK_EQ_INT(e->durationSec, 600);
        }
    }

    std::printf("resume_row: %d passed, %d failed\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
