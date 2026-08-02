#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace cs {

// Abstract HTTP client. Production wires to libcross2d's Browser; tests
// provide an in-memory fake. Browser is synchronous (one CURL handle,
// one response buffer), so all calls go through a single worker thread
// owned by CatalogApi::Impl. NEVER call HttpClient from outside that
// worker thread — it is not thread-safe.
class HttpClient {
public:
    virtual ~HttpClient() = default;

    // GET `url`. On success returns the response body.
    // On failure returns an empty string and sets `errorOut` to a
    // human-readable description.
    virtual std::string get(const std::string &url, std::string &errorOut) = 0;

    // GET `url` into a byte buffer. Used for poster images.
    // `contentTypeOut` (e.g. "image/jpeg") is best-effort.
    // Returns true on success, false + sets `errorOut` on failure.
    virtual bool getBytes(const std::string &url,
                          std::vector<std::uint8_t> &bytesOut,
                          std::string &contentTypeOut,
                          std::string &errorOut) = 0;
};

} // namespace cs
