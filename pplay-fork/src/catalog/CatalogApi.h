#pragma once

#include <functional>
#include <string>
#include <utility>
#include <vector>

namespace cs {

struct SearchItem {
    std::string id;
    std::string provider;
    std::string type;
    std::string title;
    int year = 0;
    std::string poster;
    std::string url;
};

struct ContentItem {
    std::string id;
    std::string type;
    std::string title;
    std::string description;
    std::string poster;
    std::vector<std::pair<std::string, std::string>> translations;
    struct Episode { int number; std::string id; std::string title; };
    struct Season { int number; std::vector<Episode> episodes; };
    std::vector<Season> seasons;
};

struct StreamInfo {
    std::string url;
    std::string type;
    std::vector<std::pair<std::string, std::string>> headers;
};

class CatalogApi {
public:
    explicit CatalogApi(std::string baseUrl);

    using SearchCb = std::function<void(bool ok, std::vector<SearchItem> results, std::string error)>;
    using ContentCb = std::function<void(bool ok, ContentItem item, std::string error)>;
    using StreamCb = std::function<void(bool ok, StreamInfo info, std::string error)>;

    void searchAsync(const std::string &query, SearchCb cb);
    void contentAsync(const std::string &id, ContentCb cb);
    void streamAsync(const std::string &id, const std::string &translation, StreamCb cb);

    static std::vector<SearchItem> parseSearch(const std::string &raw);
    static ContentItem parseContent(const std::string &raw);
    static StreamInfo parseStream(const std::string &raw);

private:
    std::string baseUrl_;
};

} // namespace cs
