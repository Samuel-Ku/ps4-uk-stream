// Tests for the memory policy (issue #74, v3 spec §7).
//
// Seam under test: cs::shouldRememberMemory — series-form content
// (seasons present) is remembered; movies (no seasons) are not, regardless
// of the STYLE tag on `type` (anime/cartoon/dorama are styles, orthogonal
// to form).

#include "CatalogApi.h"

#include <cstdio>

namespace {

int g_passed = 0;
int g_failed = 0;

#define CHECK(cond) do { \
    if (cond) { ++g_passed; } \
    else { ++g_failed; std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } \
} while (0)

} // namespace

int main() {
    // Series with seasons → remembered.
    {
        cs::ContentItem series;
        cs::ContentItem::Season s;
        s.number = 1;
        cs::ContentItem::Episode ep;
        ep.number = 1;
        s.episodes.push_back(ep);
        series.seasons.push_back(s);
        CHECK(cs::shouldRememberMemory(series));
    }

    // Plain series (no style tag) with seasons → remembered.
    {
        cs::ContentItem series;
        cs::ContentItem::Season s;
        series.seasons.push_back(s);
        CHECK(cs::shouldRememberMemory(series));
    }

    // Movie without seasons, anime style tag → NOT remembered (the bug).
    {
        cs::ContentItem movie;
        movie.type = "anime";
        CHECK(!cs::shouldRememberMemory(movie));
    }

    // Movie without seasons, other style tags → NOT remembered.
    {
        cs::ContentItem movie;
        movie.type = "cartoon";
        CHECK(!cs::shouldRememberMemory(movie));

        cs::ContentItem movie2;
        movie2.type = "dorama";
        CHECK(!cs::shouldRememberMemory(movie2));
    }

    // Movie without seasons, type = "movie" → NOT remembered.
    {
        cs::ContentItem movie;
        movie.type = "movie";
        CHECK(!cs::shouldRememberMemory(movie));
    }

    // No seasons, no type at all → NOT remembered.
    {
        cs::ContentItem empty;
        CHECK(!cs::shouldRememberMemory(empty));
    }

    std::printf("memory_policy: %d passed, %d failed\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
