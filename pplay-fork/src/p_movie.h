// PS4-local stub for pscrap's p_movie.h. The real p_movie.h lives in
// pscrap/include/ and the scrapper subdir is excluded from the PS4
// build (libcurl/json-c deps), so the real header isn't on the
// include path. The MediaFile / scrap_view headers #include
// "p_movie.h" with the quoted form, which searches the source file's
// directory first; placing a stub here satisfies that without needing
// any pscrap/ plumbing in the PS4 link.
//
// The stub mirrors the original struct layout (used as a value type in
// `std::vector<pscrap::Movie> movies` in MediaFile) so the existing
// non-pscrap code that includes p_movie.h still type-checks. The
// getPoster / getBackdrop methods are omitted because no PS4 TU calls
// them and the real implementations would pull in libcurl.

#ifndef PPLAY_P_MOVIE_H_PS4_STUB
#define PPLAY_P_MOVIE_H_PS4_STUB

#include <string>
#include <vector>

namespace pscrap {
    class Movie {
    public:
        int vote_count = 0;
        int id = 0;
        bool video = false;
        double vote_average = 0.0;
        std::string title;
        double popularity = 0.0;
        std::string poster_path;
        std::string original_language;
        std::string original_title;
        std::vector<int> genre_ids;
        std::string backdrop_path;
        bool adult = false;
        std::string overview;
        std::string release_date;
    };
}

#endif // PPLAY_P_MOVIE_H_PS4_STUB
