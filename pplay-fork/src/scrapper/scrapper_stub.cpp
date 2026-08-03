// PS4 stub for the pplay Scrapper. The real Scrapper fetches
// TMDB/IMDB metadata via libcurl + json-c, and OpenOrbisSDK ships
// neither, so the catalog backend (a separate Linux service) does
// the metadata lookup out of band. pplay-fork's main.h unconditionally
// constructs and destructs a `pplay::Scrapper` member, so this stub
// provides a no-op implementation that satisfies the linker. See
// CMakeLists.txt (PLATFORM_PS4 branch) for the source swap.

#include "scrapper.h"

namespace pplay {
    Scrapper::Scrapper(Main *) {
        // No-op: scrapper is disabled on PS4.
    }
    Scrapper::~Scrapper() {
        // No-op.
    }
    int Scrapper::scrap(const std::string &) {
        return -1;
    }
}
