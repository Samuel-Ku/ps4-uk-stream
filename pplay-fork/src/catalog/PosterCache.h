#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace cs {

// On-disk poster cache (issue #54, v3 spec §8). Files live at
//   <rootDir>/<hash(url)>.<ext>
// where ext encodes the stored content type. Writes are atomic
// (tmp file + rename). TTL is enforced on read via file mtime;
// expired entries simply miss (a successful refetch overwrites them).
//
// All methods are best-effort: IO failures never throw and never
// break serving — a broken cache just always misses.
class DiskPosterCache {
public:
    explicit DiskPosterCache(std::string rootDir,
                             std::int64_t ttlSeconds = 7LL * 24 * 3600);

    // True on a fresh hit; fills bytesOut/contentTypeOut. False on miss,
    // expiry, or any IO/validation problem. Content types that are not
    // recognized image types normalize to image/jpeg (backend contract).
    bool get(const std::string &url,
             std::vector<std::uint8_t> &bytesOut,
             std::string &contentTypeOut) const;

    // Store bytes under the URL key. Never throws.
    void put(const std::string &url,
             const std::vector<std::uint8_t> &bytes,
             const std::string &contentType) const;

    // The file key for a URL (hash of the exact URL string).
    static std::string fileKey(const std::string &url);

private:
    std::string rootDir_;
    std::int64_t ttlSeconds_;
};

} // namespace cs
