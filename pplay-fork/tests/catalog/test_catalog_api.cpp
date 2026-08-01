#include <cstdio>
#include "CatalogApi.h"

int main() {
    const char *raw = R"({"query":"Дюна","results":[
        {"id":"uakino:1","provider":"uakino","type":"movie","title":"Дюна","year":2021,"poster":"/p","url":"https://x"}
    ]})";
    auto parsed = cs::CatalogApi::parseSearch(raw);
    if (parsed.empty()) { std::fprintf(stderr, "empty\n"); return 1; }
    if (parsed[0].title != "Дюна") { std::fprintf(stderr, "title\n"); return 2; }
    if (parsed[0].year != 2021) { std::fprintf(stderr, "year\n"); return 3; }
    if (parsed[0].id != "uakino:1") { std::fprintf(stderr, "id\n"); return 4; }
    return 0;
}
