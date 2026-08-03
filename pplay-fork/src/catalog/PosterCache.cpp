#include "PosterCache.h"

#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <functional>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

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

// POSIX replacements for the std::filesystem primitives the original
// implementation used. We deliberately avoid <filesystem> here because
// the PS4 toolchain's libc++ (FreeBSD 12.0 era) ships a broken
// <filesystem> header that transitively pulls in <locale>, which then
// fails to find `locale_t` / `nanosleep` / `isascii` (musl xlocale.h
// not exposed in the OpenOrbisSDK sysroot). The standalone harness
// (Linux libc++) is unaffected; this file still builds cleanly there
// because the POSIX calls are universally available.

bool isDir(const std::string &p) {
    struct stat st;
    if (::stat(p.c_str(), &st) != 0) return false;
    return S_ISDIR(st.st_mode);
}

bool isRegularFile(const std::string &p) {
    struct stat st;
    if (::stat(p.c_str(), &st) != 0) return false;
    return S_ISREG(st.st_mode);
}

bool mtimeWithinTtl(const std::string &p, std::int64_t ttlSeconds) {
    struct stat st;
    if (::stat(p.c_str(), &st) != 0) return false;
    const auto age = std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::system_clock::now().time_since_epoch() -
        std::chrono::seconds(st.st_mtime)).count();
    // Negative age means mtime is in the future (clock skew). Treat as
    // fresh so we don't loop refetching.
    return age < 0 || age <= ttlSeconds;
}

bool mkdirOne(const std::string &p) {
    if (::mkdir(p.c_str(), 0755) == 0) return true;
    return errno == EEXIST;
}

// Walk up from `p` until a parent that exists, then mkdir down. We
// could call ::mkdir in a loop with errno==EEXIST, but `rootDir_` is
// passed in as a literal from main and almost always already exists;
// a single mkdir() per directory in the chain is plenty.
bool mkdirRecursive(const std::string &p) {
    if (p.empty()) return false;
    std::string acc;
    acc.reserve(p.size());
    // Skip leading separators so we don't try to mkdir("/").
    std::size_t i = 0;
    while (i < p.size() && p[i] == '/') {
        acc.push_back(p[i]);
        ++i;
    }
    while (i < p.size()) {
        if (p[i] == '/') {
            if (!acc.empty() && !mkdirOne(acc)) return false;
        }
        acc.push_back(p[i]);
        ++i;
    }
    return mkdirOne(acc);
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
    if (!isDir(rootDir_)) return false;

    const std::string stem = rootDir_ + "/" + fileKey(url);
    for (const auto &e : kKnown) {
        const std::string candidate = stem + e.ext;
        if (!isRegularFile(candidate)) continue;
        if (!mtimeWithinTtl(candidate, ttlSeconds_)) return false;  // expired — force refetch
        std::ifstream f(candidate, std::ios::binary);
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
    if (!mkdirRecursive(rootDir_)) return;

    const std::string ext = extForType(contentType);
    const std::string finalPath = rootDir_ + "/" + fileKey(url) + ext;
    // Atomic write, repo convention: <final>.tmp (no hidden-dot prefix).
    const std::string tmpPath = rootDir_ + "/" + fileKey(url) + ".tmp";
    {
        std::ofstream f(tmpPath, std::ios::binary | std::ios::trunc);
        if (!f) return;
        f.write(reinterpret_cast<const char *>(bytes.data()),
                static_cast<std::streamsize>(bytes.size()));
        if (!f.good()) {
            f.close();
            ::unlink(tmpPath.c_str());
            return;
        }
    }
    // One URL = one file: remove sibling extensions so a stale .jpg can
    // never shadow a newer .png of the same poster.
    for (const auto &e : kKnown) {
        if (std::string(e.ext) != ext) {
            ::unlink((rootDir_ + "/" + fileKey(url) + e.ext).c_str());
        }
    }
    if (::rename(tmpPath.c_str(), finalPath.c_str()) != 0) {
        ::unlink(tmpPath.c_str());
    }
}

} // namespace cs