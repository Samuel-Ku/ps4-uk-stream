#pragma once

#include "HttpClient.h"

#include <cstdint>
#include <map>
#include <string>
#include <utility>
#include <vector>

namespace cs_test {

// Test-only HttpClient. Routes are matched by URL prefix (longest match
// wins); unmatched URLs return "fake: no route for ..." so the test
// can distinguish a missing route from a configured error.
class FakeHttpClient : public cs::HttpClient {
public:
    // Add a JSON/text body route. Subsequent `set` calls on the same
    // prefix overwrite; this is intentional for state-machine tests.
    void set(const std::string &urlPrefix, const std::string &body) {
        routes_[urlPrefix] = body;
    }
    // Add a bytes route (for posters). `contentType` is returned verbatim.
    void setBytes(const std::string &urlPrefix,
                  const std::vector<std::uint8_t> &bytes,
                  const std::string &contentType) {
        bytes_[urlPrefix] = std::make_pair(bytes, contentType);
    }
    // Add an error route - `get` returns empty + sets errorOut.
    void setError(const std::string &urlPrefix, const std::string &error) {
        errors_[urlPrefix] = error;
    }
    int getCount() const { return getCount_; }
    int getBytesCount() const { return getBytesCount_; }
    // Last URL passed to get() — tests use this to assert the C++
    // builder hits the exact endpoint the backend expects (e.g.
    // "/api/content/<urlencoded-id>" rather than "?id=...").
    std::string lastUrl() const { return lastUrl_; }

    std::string get(const std::string &url, std::string &errorOut) override {
        ++getCount_;
        lastUrl_ = url;
        const std::string *best = longestMatch(url, routes_);
        if (!best) {
            // Empty errorOut is the convention used by CatalogApi to
            // treat empty-body + empty-error as a network failure.
            // The fake leaves errorOut empty so the production code
            // path (empty body => error_network) takes over.
            errorOut.clear();
            return {};
        }
        auto eIt = errors_.find(*best);
        if (eIt != errors_.end() && !eIt->second.empty()) {
            errorOut = eIt->second;
            return {};
        }
        return routes_.at(*best);
    }

    bool getBytes(const std::string &url,
                  std::vector<std::uint8_t> &bytesOut,
                  std::string &ctOut,
                  std::string &errorOut) override {
        ++getBytesCount_;
        const std::string *best = longestMatch(url, bytes_);
        if (!best) { errorOut.clear(); return false; }
        bytesOut = bytes_.at(*best).first;
        ctOut = bytes_.at(*best).second;
        return true;
    }

private:
    template <typename Map>
    static const std::string *longestMatch(const std::string &url, const Map &m) {
        // Substring match (longest wins). URL-encoding is opaque to the
        // fake — the route only needs to identify the endpoint, not the
        // exact query string. Tests that need exact-match discrimination
        // can set a longer / more specific prefix.
        const std::string *best = nullptr;
        for (const auto &kv : m) {
            const std::string &prefix = kv.first;
            if (url.find(prefix) != std::string::npos) {
                if (!best || prefix.size() > best->size()) best = &prefix;
            }
        }
        return best;
    }

    std::map<std::string, std::string> routes_;
    std::map<std::string, std::pair<std::vector<std::uint8_t>, std::string>> bytes_;
    std::map<std::string, std::string> errors_;
    int getCount_ = 0;
    int getBytesCount_ = 0;
    std::string lastUrl_;
};

} // namespace cs_test
