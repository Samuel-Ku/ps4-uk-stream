#include "CatalogApi.h"
#include "Json.h"
#include <cctype>
#include <cstdio>
#include <utility>

namespace cs {

CatalogApi::CatalogApi(std::string baseUrl) : baseUrl_(std::move(baseUrl)) {}

[[maybe_unused]] static std::string urlEncode(const std::string &s) {
    std::string out;
    out.reserve(s.size());
    for (unsigned char c : s) {
        if (std::isalnum(c) || c=='-'||c=='_'||c=='.'||c=='~') {
            out += static_cast<char>(c);
        } else {
            char buf[4];
            std::snprintf(buf, sizeof(buf), "%%%02X", c);
            out += buf;
        }
    }
    return out;
}

void CatalogApi::searchAsync(const std::string &query, SearchCb cb) {
    (void)query; (void)cb;
    std::fprintf(stderr, "CatalogApi::searchAsync not implemented yet (network wired in Task 13)\n");
}

void CatalogApi::contentAsync(const std::string &id, ContentCb cb) {
    (void)id; (void)cb;
    std::fprintf(stderr, "CatalogApi::contentAsync not implemented yet (network wired in Task 13)\n");
}

void CatalogApi::streamAsync(const std::string &id, const std::string &translation, StreamCb cb) {
    (void)id; (void)translation; (void)cb;
    std::fprintf(stderr, "CatalogApi::streamAsync not implemented yet (network wired in Task 13)\n");
}

std::vector<SearchItem> CatalogApi::parseSearch(const std::string &raw) {
    std::vector<SearchItem> out;
    auto doc = JsonDoc::parse(raw);
    if (!doc) return out;
    for (const auto &v : doc->root().arr("results")) {
        SearchItem it;
        it.id = v.str("id");
        it.provider = v.str("provider");
        it.type = v.str("type");
        it.title = v.str("title");
        it.year = v.integer("year", 0);
        it.poster = v.str("poster");
        it.url = v.str("url");
        if (!it.id.empty()) out.push_back(std::move(it));
    }
    return out;
}

ContentItem CatalogApi::parseContent(const std::string &raw) {
    ContentItem out;
    auto doc = JsonDoc::parse(raw);
    if (!doc) return out;
    auto r = doc->root();
    out.id = r.str("id");
    out.type = r.str("type");
    out.title = r.str("title");
    out.description = r.str("description");
    out.poster = r.str("poster");
    for (const auto &t : r.arr("translations")) {
        out.translations.emplace_back(t.str("id"), t.str("label"));
    }
    int seasonNo = 0;
    for (const auto &s : r.arr("seasons")) {
        ContentItem::Season cs2;
        cs2.number = s.integer("number", ++seasonNo);
        int epNo = 0;
        for (const auto &e : s.arr("episodes")) {
            ContentItem::Episode ep;
            ep.number = e.integer("number", ++epNo);
            ep.id = e.str("id");
            ep.title = e.str("title");
            cs2.episodes.push_back(std::move(ep));
        }
        out.seasons.push_back(std::move(cs2));
    }
    return out;
}

StreamInfo CatalogApi::parseStream(const std::string &raw) {
    StreamInfo out;
    auto doc = JsonDoc::parse(raw);
    if (!doc) return out;
    out.url = doc->root().str("url");
    out.type = doc->root().str("type");
    return out;
}

} // namespace cs
