// PS4-local stub for pscrap's p_search.h. The real p_search.h lives
// in pscrap/include/ and the scrapper subdir is excluded from the
// PS4 build (libcurl/json-c deps), so the real header isn't on the
// include path. The scrapper.cpp source #includes "p_search.h" with
// the quoted form, which searches the source file's directory first;
// placing a stub here satisfies that without needing any pscrap/
// plumbing in the PS4 link. The scrapper is a feature we don't ship
// on PS4 (catalog runs on a separate Linux service), so the stub
// only needs the type shape for the include chain to compile.

#ifndef PPLAY_P_SEARCH_H_PS4_STUB
#define PPLAY_P_SEARCH_H_PS4_STUB

#include <string>
#include <vector>

namespace pscrap {
    struct SearchResult {
        int id = 0;
        std::string title;
        std::string poster_path;
        std::string backdrop_path;
        std::string overview;
        std::string release_date;
        double vote_average = 0.0;
    };

    class Search {
    public:
        std::vector<Movie> movies;
        int total_results = 0;
        int load(const std::string &, int = 1) { return -1; }
        int query(const std::string &q, std::vector<SearchResult> &out) { (void)q; out.clear(); return 0; }
        int save(const std::string &, int = 1) { return -1; }
    };
}

#endif // PPLAY_P_SEARCH_H_PS4_STUB
