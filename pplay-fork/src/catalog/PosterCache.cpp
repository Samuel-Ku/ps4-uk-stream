#include "PosterCache.h"

#include <cerrno>
#include <cstdio>
#include <fstream>
#include <functional>
#include <system_error>

#include <filesystem>

namespace fs = std::filesystem;

namespace cs {

namespace {

constexpr std::int64_t kDefaultTtlSeconds = 7LL * 24 * 3600;

struct ExtType {
    const char *ext;
    const char *type;
};

constexpr ExtType kKnown[] = {
    {".jpg", "image/jpeg"},
    {".png", "image/png"},
    {".webp", "image/webp"},
    {".gif", "image/gif"},
};

const char *extForType(const std::string &contentType) {
    for (const auto &e : kKnown) {
        if (contentType == e.type) return e.ext;
    }
    return ".jpg";
}

const char *typeForExt(const std::string &ext) {
    for (const auto &e : kKnown) {
        if (ext == e.ext) return e.type;
    }
    return nullptr;
}

bool isFresh(const fs::path &p, std::int64_t ttlSeconds) {
    std::error_code ec;
    const auto mtime = fs::last_write_time(p, ec);
    if (ec) return false;
    const auto age = fs::file_time_type::clock::now() - mtime;
    return std::chrono::duration_cast<std::chrono::seconds>(age).count() <= ttlSeconds;
}

} // namespace

DiskPosterCache::DiskPosterCache(std::string rootDir, std::int64_t ttlSeconds)
    : rootDir_(std::move(rootDir)),
      ttlSeconds_(ttlSeconds < 0 ? kDefaultTtlSeconds : ttlSeconds) {}

std::string DiskPosterCache::fileKey(const std::string &url) {
    // Same hashing approach the rest of pPlay's cache folders already use
    // (Utility::getMediaPosterPath): stable within one binary build, which
    // is all a per-console disk cache needs.
    return std::to_string(std::hash<std::string>()(url));
}

bool DiskPosterCache::get(const std::string &url,
                          std::vector<std::uint8_t> &bytesOut,
                          std::string &contentTypeOut) const {
    std::error_code ec;
    if (!fs::is_directory(rootDir_, ec) || ec) return false;

    const std::string stem = rootDir_ + "/" + fileKey(url);
    for (const auto &e : kKnown) {
        const fs::path p(stem + e.ext);
        if (!fs::is_regular_file(p, ec) || ec) continue;
        if (!isFresh(p, ttlSeconds_)) return false;  // expired — force refetch
        std::ifstream f(p, std::ios::binary);
        if (!f) return false;
        bytesOut.assign(std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>());
        contentTypeOut = e.type;
        return !bytesOut.empty();
    }
    return false;
}

void DiskPosterCache::put(const std::string &url,
                          const std::vector<std::uint8_t> &bytes,
                          const std::string &contentType) const {
    if (bytes.empty()) return;
    std::error_code ec;
    fs::create_directories(rootDir_, ec);
    if (ec) return;

    const std::string ext = extForType(contentType);
    const fs::path finalPath(rootDir_ + "/" + fileKey(url) + ext);
    // Atomic write, repo convention: <final>.tmp (no hidden-dot prefix).
    const fs::path tmpPath(rootDir_ + "/" + fileKey(url) + ".tmp");
    {
        std::ofstream f(tmpPath, std::ios::binary | std::ios::trunc);
        if (!f) return;
        f.write(reinterpret_cast<const char *>(bytes.data()),
                static_cast<std::streamsize>(bytes.size()));
        if (!f.good()) {
            f.close();
            fs::remove(tmpPath, ec);
            return;
        }
    }
    // One URL = one file: remove sibling extensions so a stale .jpg can
    // never shadow a newer .png of the same poster.
    for (const auto &e : kKnown) {
        if (std::string(e.ext) != ext) {
            fs::remove(rootDir_ + "/" + fileKey(url) + e.ext, ec);
        }
    }
    fs::rename(tmpPath, finalPath, ec);
    if (ec) fs::remove(tmpPath, ec);
}

} // namespace cs
