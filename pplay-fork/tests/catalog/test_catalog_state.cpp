// Tests for the client state store (issue #55, v3 spec §7).
//
// Seam under test: cs::CatalogState — JSON roundtrip, upsert semantics,
// LRU-50 eviction, corruption tolerance, isFinished predicate.

#include "CatalogState.h"

#include <cstdio>
#include <filesystem>
#include <fstream>
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

fs::path freshTmpPath(const std::string &tag) {
    fs::path p = fs::temp_directory_path() /
                 ("pplay_state_test_" + tag + "_" + std::to_string(static_cast<long long>(std::rand())));
    fs::remove(p);
    return p;
}

cs::ResumeEntry resume(const std::string &group, long pos, long dur, std::int64_t at) {
    cs::ResumeEntry e;
    e.groupKey = group;
    e.provider = "uakino";
    e.id = "uakino:1";
    e.episodeId = "uakino:1:e3";
    e.translationLabel = "Українська";
    e.positionSec = pos;
    e.durationSec = dur;
    e.updatedAt = at;
    return e;
}

} // namespace

int main() {
    // --- missing file is fine, stays empty --------------------------------
    {
        fs::path p = freshTmpPath("missing");
        cs::CatalogState st(p.string());
        CHECK(!st.load());
        CHECK(st.resume("g1:zzz") == nullptr);
        CHECK(st.memory("g1:zzz") == nullptr);
        fs::remove(p);
    }

    // --- roundtrip ---------------------------------------------------------
    {
        fs::path p = freshTmpPath("roundtrip");
        {
            cs::CatalogState st(p.string());
            st.setResume(resume("g1:a", 123, 1400, 1000));
            cs::MemoryEntry m;
            m.groupKey = "g1:a";
            m.provider = "uakino";
            m.translationLabel = "Українська";
            m.updatedAt = 1000;
            st.setMemory(m);
            CHECK(st.save());
        }
        cs::CatalogState st2(p.string());
        CHECK(st2.load());
        const auto *r = st2.resume("g1:a");
        CHECK(r != nullptr);
        if (r) {
            CHECK_EQ(r->provider, std::string("uakino"));
            CHECK_EQ(r->id, std::string("uakino:1"));
            CHECK_EQ(r->episodeId, std::string("uakino:1:e3"));
            CHECK_EQ(r->translationLabel, std::string("Українська"));
            CHECK_EQ(r->positionSec, 123L);
            CHECK_EQ(r->durationSec, 1400L);
            CHECK_EQ(r->updatedAt, (std::int64_t) 1000);
        }
        const auto *m = st2.memory("g1:a");
        CHECK(m != nullptr);
        if (m) CHECK_EQ(m->provider, std::string("uakino"));
        fs::remove(p);
    }

    // --- upsert by group ---------------------------------------------------
    {
        fs::path p = freshTmpPath("upsert");
        cs::CatalogState st(p.string());
        st.setResume(resume("g1:a", 10, 100, 1000));
        st.setResume(resume("g1:a", 50, 100, 2000));
        CHECK_EQ(st.recentResume(50).size(), (size_t) 1);
        CHECK_EQ(st.resume("g1:a")->positionSec, 50L);
        fs::remove(p);
    }

    // --- recency ordering --------------------------------------------------
    {
        fs::path p = freshTmpPath("recent");
        cs::CatalogState st(p.string());
        st.setResume(resume("g1:old", 10, 100, 100));
        st.setResume(resume("g1:new", 10, 100, 300));
        st.setResume(resume("g1:mid", 10, 100, 200));
        auto r = st.recentResume(20);
        CHECK_EQ(r.size(), (size_t) 3);
        CHECK_EQ(r[0].groupKey, std::string("g1:new"));
        CHECK_EQ(r[2].groupKey, std::string("g1:old"));
        fs::remove(p);
    }

    // --- LRU cap of 50 ------------------------------------------------------
    {
        fs::path p = freshTmpPath("lru");
        cs::CatalogState st(p.string());
        for (int i = 0; i < 55; ++i) {
            st.setResume(resume("g1:" + std::to_string(i), 10, 100, 1000 + i));
        }
        CHECK(st.save());
        cs::CatalogState st2(p.string());
        CHECK(st2.load());
        CHECK_EQ(st2.recentResume(100).size(), cs::CatalogState::kMaxEntries);
        // Oldest 5 evicted:
        CHECK(st2.resume("g1:0") == nullptr);
        CHECK(st2.resume("g1:4") == nullptr);
        CHECK(st2.resume("g1:5") != nullptr);
        fs::remove(p);
    }

    // --- isFinished predicate (>= 95% watched) ------------------------------
    {
        CHECK(cs::CatalogState::isFinished(resume("g1:x", 95, 100, 0)));
        CHECK(!cs::CatalogState::isFinished(resume("g1:x", 94, 100, 0)));
        CHECK(!cs::CatalogState::isFinished(resume("g1:x", 0, 0, 0)));  // no duration
    }

    // --- memory upsert by group --------------------------------------------
    {
        fs::path p = freshTmpPath("memory");
        cs::CatalogState st(p.string());
        cs::MemoryEntry m;
        m.groupKey = "g1:a";
        m.provider = "uakino";
        m.translationLabel = "дуб";
        st.setMemory(m);
        m.provider = "ufdub";
        st.setMemory(m);
        CHECK_EQ(st.memory("g1:a")->provider, std::string("ufdub"));
        CHECK(st.save());
        cs::CatalogState st2(p.string());
        CHECK(st2.load());
        CHECK_EQ(st2.memory("g1:a")->provider, std::string("ufdub"));
        fs::remove(p);
    }

    // --- corrupt file: tolerate, then self-heal ------------------------------
    {
        fs::path p = freshTmpPath("corrupt");
        { std::ofstream f(p); f << "{ this is not json ]"; }
        cs::CatalogState st(p.string());
        CHECK(!st.load());
        CHECK(st.resume("g1:a") == nullptr);
        st.setResume(resume("g1:a", 1, 2, 3));
        CHECK(st.save());
        cs::CatalogState st2(p.string());
        CHECK(st2.load());
        CHECK(st2.resume("g1:a") != nullptr);
        fs::remove(p);
    }

    // --- atomic save leaves no tmp litter -----------------------------------
    {
        fs::path p = freshTmpPath("atomic");
        cs::CatalogState st(p.string());
        st.setResume(resume("g1:a", 1, 2, 3));
        CHECK(st.save());
        CHECK(fs::exists(p));
        CHECK(!fs::exists(p.string() + ".tmp"));
        fs::remove(p);
    }

    std::printf("catalog_state: %d passed, %d failed\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
