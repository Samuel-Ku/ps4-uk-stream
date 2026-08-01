#include <cassert>
#include <cstdio>
#include "Json.h"

int main() {
    const char *raw = R"({"q":"Дюна","results":[{"id":"uakino:1","title":"Дюна","type":"movie"}]})";
    auto doc = cs::JsonDoc::parse(raw);
    if (!doc) { std::fprintf(stderr, "parse failed\n"); return 1; }
    if (doc->root().str("q") != "Дюна") { std::fprintf(stderr, "q mismatch\n"); return 2; }
    auto arr = doc->root().arr("results");
    if (arr.size() != 1) { std::fprintf(stderr, "results size\n"); return 3; }
    if (arr[0].str("id") != "uakino:1") { std::fprintf(stderr, "id mismatch\n"); return 4; }
    if (arr[0].str("type") != "movie") { std::fprintf(stderr, "type mismatch\n"); return 5; }
    return 0;
}
