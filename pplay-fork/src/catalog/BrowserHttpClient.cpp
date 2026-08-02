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
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <unistd.h>
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
        // Browser's write_bytes() only opens a FILE* for subsequent
        // requests — it does NOT flush the in-memory html_response
        // buffer to disk. So we: (1) load the body into html_response,
        // (2) write that string to a temp file via C++ ofstream, (3)
        // read the temp file back into bytesOut. This sidesteps the
        // Browser::write_bytes filepipe==NULL assert on read-only FSes
        // (HIGH review finding).
        // Unique tmp name per call avoids races between concurrent
        // Browse + Search posters hitting the same path.
        char tmpName[] = "/tmp/cs_catalog_poster_XXXXXX";
        const int fd = ::mkstemp(tmpName);
        if (fd < 0) {
            errorOut = "error_write_bytes";
            return false;
        }
        ::close(fd);
        const std::string tmp(tmpName);

        browser_.open_novisit(url, 12);
        if (browser_.error()) {
            errorOut = browser_.getError();
            if (errorOut.empty()) errorOut = "error_network";
            return false;
        }

        const std::string body = browser_.response();
        {
            std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
            if (!out) {
                errorOut = "error_write_bytes";
                return false;
            }
            out.write(body.data(), static_cast<std::streamsize>(body.size()));
        }

        std::ifstream in(tmp, std::ios::binary);
        if (!in) {
            errorOut = "error_write_bytes";
            std::remove(tmp.c_str());
            return false;
        }
        bytesOut.assign(std::istreambuf_iterator<char>(in),
                        std::istreambuf_iterator<char>());
        std::remove(tmp.c_str());

        // Content-Type is best-effort: pull it from the response header.
        // RFC 7230 says header field names are case-insensitive, so scan
        // for both casings.
        contentTypeOut.clear();
        const std::string info = browser_.info();
        constexpr std::string_view kCtKeys[] = {"Content-Type:", "content-type:"};
        std::size_t pos = std::string::npos;
        for (const auto &key : kCtKeys) {
            pos = info.find(key);
            if (pos != std::string::npos) {
                pos += key.size();
                break;
            }
        }
        if (pos != std::string::npos && pos < info.size()) {
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
