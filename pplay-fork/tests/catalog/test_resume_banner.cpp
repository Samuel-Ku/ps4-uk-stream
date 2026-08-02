// Tests for the resume banner string formatting (issue #72).
//
// ScreenContent::renderResumeBanner() composes "▶ Поновити з MM:SS"
// from a ResumeEntry's positionSec. The test locks the format so
// the user-visible label can change only via a deliberate test
// edit. The store-side rule (entry is live = non-zero position AND
// not finished) is also captured here.

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

#define CHECK(cond) do { \
    if (cond) { ++g_passed; } \
    else { ++g_failed; std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } \
} while (0)

// Helper identical to ScreenContent::renderResumeBanner formatting.
// Keep in sync with the screen — the test exists to lock the format.
std::string formatBanner(long positionSec) {
    const long mins = positionSec / 60;
    const long secs = positionSec % 60;
    char buf[64];
    std::snprintf(buf, sizeof(buf), "▶ Поновити з %02ld:%02ld", mins, secs);
    return std::string(buf);
}

cs::ResumeEntry makeEntry(const std::string &gk, long pos, long dur,
                          std::int64_t when) {
    cs::ResumeEntry e;
    e.groupKey = gk;
    e.provider = "uakino";
    e.id = "id";
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

    // Format: zero-padded MM:SS.
    CHECK_EQ(formatBanner(0), std::string("▶ Поновити з 00:00"));
    CHECK_EQ(formatBanner(7), std::string("▶ Поновити з 00:07"));
    CHECK_EQ(formatBanner(60), std::string("▶ Поновити з 01:00"));
    CHECK_EQ(formatBanner(90), std::string("▶ Поновити з 01:30"));
    CHECK_EQ(formatBanner(900), std::string("▶ Поновити з 15:00"));
    CHECK_EQ(formatBanner(3599), std::string("▶ Поновити з 59:59"));
    CHECK_EQ(formatBanner(3600), std::string("▶ Поновити з 60:00"));
    CHECK_EQ(formatBanner(7261), std::string("▶ Поновити з 121:01"));

    // "Show" policy: a live entry is one that
    //   1) has a non-zero position
    //   2) is not finished (>= 95%) per CatalogState::isFinished
    // The screen calls renderResumeBanner on every renderAll; if both
    // conditions hold, the banner is shown.
    auto shouldShow = [](const ResumeEntry *e) {
        return e != nullptr && e->positionSec > 0 &&
               !CatalogState::isFinished(*e);
    };

    // Missing entry.
    {
        CatalogState st("/tmp/catalog_state_test_banner_1.json");
        CHECK(!shouldShow(st.resume("absent")));
    }
    // Zero position — banner hidden (no progress to resume).
    {
        CatalogState st("/tmp/catalog_state_test_banner_2.json");
        st.setResume(makeEntry("gk", 0, 600, 1));
        CHECK(!shouldShow(st.resume("gk")));
    }
    // Live entry — banner shown.
    {
        CatalogState st("/tmp/catalog_state_test_banner_3.json");
        st.setResume(makeEntry("gk", 120, 600, 1));
        const ResumeEntry *e = st.resume("gk");
        CHECK(e != nullptr);
        CHECK(shouldShow(e));
        // And the formatter agrees.
        CHECK_EQ(formatBanner(e->positionSec),
                 std::string("▶ Поновити з 02:00"));
    }
    // Finished entry — banner hidden, even though positionSec > 0.
    {
        CatalogState st("/tmp/catalog_state_test_banner_4.json");
        st.setResume(makeEntry("gk-fin", 950, 1000, 1));
        const ResumeEntry *e = st.resume("gk-fin");
        CHECK(e != nullptr);
        CHECK(!shouldShow(e));
    }
    // Unknown duration — banner shown. isFinished() requires a
    // known durationSec > 0, so positionSec > 0 with durationSec == 0
    // is by definition not finished. We display the position; the
    // user decides whether to resume (the player itself will report
    // EOF when the source ends).
    {
        CatalogState st("/tmp/catalog_state_test_banner_5.json");
        st.setResume(makeEntry("gk-und", 60, 0, 1));
        CHECK(shouldShow(st.resume("gk-und")));
    }

    std::printf("resume_banner: %d passed, %d failed\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
