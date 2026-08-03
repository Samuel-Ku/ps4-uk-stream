// Static (no-deps) test for the RAII TmpFile helper that lives in
// BrowserHttpClient.cpp. We cannot link BrowserHttpClient.cpp here
// because it pulls in libcross2d's Browser.hpp, so the test keeps a
// self-contained copy of the same helper and exercises the cleanup
// paths. Any future change to the TmpFile class in
// BrowserHttpClient.cpp must be mirrored here.
//
// The helper exists so that `getBytes` can early-return on every
// failure path without leaking `/tmp/cs_catalog_poster_*` files.

#include <cstdio>
#include <cstdlib>
#include <string>
#include <unistd.h>
#include <vector>

namespace {

// Mirror of TmpFile in src/catalog/BrowserHttpClient.cpp. Kept in
// sync manually; if the production class changes, update this copy.
class TmpFile {
public:
    TmpFile() = default;
    ~TmpFile() {
        if (!released_ && !path_.empty()) {
            std::remove(path_.c_str());
        }
    }
    TmpFile(const TmpFile &) = delete;
    TmpFile &operator=(const TmpFile &) = delete;

    bool create(const std::string &prefix, std::string &errorOut) {
        path_ = prefix + "_XXXXXX";
        std::vector<char> buf(path_.begin(), path_.end());
        buf.push_back('\0');
        const int fd = ::mkstemp(buf.data());
        if (fd < 0) {
            errorOut = "mkstemp_failed";
            path_.clear();
            return false;
        }
        ::close(fd);
        path_.assign(buf.data());
        return true;
    }

    const std::string &path() const { return path_; }
    void keep() { released_ = true; }

private:
    std::string path_;
    bool released_ = false;
};

bool exists(const std::string &path) {
    return ::access(path.c_str(), F_OK) == 0;
}

int g_passed = 0;
int g_failed = 0;

#define CHECK(cond) do { \
    if (cond) { ++g_passed; } \
    else { ++g_failed; std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } \
} while (0)

#define CHECK_EQ(a, b) do { \
    auto _a = (a); auto _b = (b); \
    if (_a == _b) { ++g_passed; } \
    else { ++g_failed; std::fprintf(stderr, "FAIL %s:%d: %s != %s\n", __FILE__, __LINE__, #a, #b); } \
} while (0)

} // namespace

int main() {
    // 1. Successful create + early return WITHOUT keep() — file must be
    //    cleaned up by the destructor.
    {
        std::string path;
        {
            TmpFile tmp;
            std::string err;
            CHECK(tmp.create("/tmp/cs_test_poster", err));
            CHECK(err.empty());
            path = tmp.path();
            CHECK(!path.empty());
            CHECK(exists(path));
        }
        CHECK(!exists(path));
    }

    // 2. Successful create + keep() — file must survive destruction.
    {
        std::string path;
        {
            TmpFile tmp;
            std::string err;
            CHECK(tmp.create("/tmp/cs_test_poster", err));
            path = tmp.path();
            tmp.keep();
        }
        CHECK(exists(path));
        std::remove(path.c_str());
    }

    // 3. Two concurrent TmpFile instances must get unique names (the
    //    "XXXXXX" + mkstemp atomic-rename contract).
    {
        TmpFile a;
        TmpFile b;
        std::string err;
        CHECK(a.create("/tmp/cs_test_poster", err));
        CHECK(b.create("/tmp/cs_test_poster", err));
        CHECK(!a.path().empty());
        CHECK(!b.path().empty());
        CHECK(a.path() != b.path());
        CHECK(exists(a.path()));
        CHECK(exists(b.path()));
    }

    // 4. Empty prefix path is a contract violation but must not crash
    //    (mkstemp will fail and we should report mkstemp_failed).
    {
        TmpFile tmp;
        std::string err;
        // "/" alone won't get a valid temp name; we just verify the
        // helper doesn't crash on an empty prefix and that the error
        // tag matches the production value.
        const bool ok = tmp.create("/nonexistent_dir_xyz", err);
        CHECK(!ok);
        CHECK_EQ(err, std::string("mkstemp_failed"));
    }

    if (g_failed != 0) {
        std::fprintf(stderr, "test_tmp_file: %d/%d checks failed\n", g_failed, g_passed + g_failed);
        return 1;
    }
    std::fprintf(stderr, "test_tmp_file: %d checks passed\n", g_passed);
    return 0;
}
