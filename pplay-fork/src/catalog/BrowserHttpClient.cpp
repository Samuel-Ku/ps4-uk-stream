// Production HttpClient implementation backed by libcross2d's Browser.
// Requires libcross2d Browser.hpp — NOT compiled in the standalone harness.
//
// All calls happen on the worker thread owned by CatalogApi::Impl. Browser
// is not thread-safe (one CURL handle, one response buffer); the harness
// tests inject a fake HttpClient instead, see tests/standalone-catalog/.

#include "BrowserHttpClient.h"

#include "filer/Browser/Browser.hpp"

#include <cstdint>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace cs {

namespace {

class BrowserHttpClient final : public HttpClient {
public:
    // GET — body is returned in `response()`; `error()` reports transport
    // failures. We deliberately discard the parsed DOM (links/forms) — only
    // the raw body matters for the catalog API.
    std::string get(const std::string &url, std::string &errorOut) override {
        // 12s timeout matches the rest of the pPlay codebase.
        browser_.open_novisit(url, 12);
        if (browser_.error()) {
            errorOut = browser_.getError();
            if (errorOut.empty()) errorOut = "error_network";
            return {};
        }
        errorOut.clear();
        return browser_.response();
    }

    bool getBytes(const std::string &url,
                  std::vector<std::uint8_t> &bytesOut,
                  std::string &contentTypeOut,
                  std::string &errorOut) override {
        // Browser has no first-class bytes API: write_bytes streams the
        // response body into a file, which we then slurp back into memory.
        const std::string tmp = "/tmp/cs_catalog_poster.bin";
        std::remove(tmp.c_str());

        // First load the body into Browser's internal buffer…
        browser_.open_novisit(url, 12);
        if (browser_.error()) {
            errorOut = browser_.getError();
            if (errorOut.empty()) errorOut = "error_network";
            return false;
        }
        // …then persist via write_bytes to the same handle, and read back.
        browser_.write_bytes(tmp);

        std::ifstream in(tmp, std::ios::binary);
        if (!in) {
            errorOut = "error_write_bytes";
            return false;
        }
        bytesOut.assign(std::istreambuf_iterator<char>(in),
                        std::istreambuf_iterator<char>());
        std::remove(tmp.c_str());

        // Content-Type is best-effort: pull it from the response header.
        contentTypeOut.clear();
        const std::string info = browser_.info();
        const std::string needle = "Content-Type:";
        auto pos = info.find(needle);
        if (pos != std::string::npos) {
            pos += needle.size();
            while (pos < info.size() && (info[pos] == ' ' || info[pos] == '\t')) ++pos;
            auto end = info.find_first_of("\r\n", pos);
            contentTypeOut = info.substr(pos, end == std::string::npos
                                              ? std::string::npos
                                              : end - pos);
        }
        if (contentTypeOut.empty()) contentTypeOut = "application/octet-stream";

        return true;
    }

private:
    Browser browser_;
};

} // namespace

std::unique_ptr<HttpClient> makeBrowserHttpClient() {
    return std::unique_ptr<HttpClient>(new BrowserHttpClient());
}

} // namespace cs
