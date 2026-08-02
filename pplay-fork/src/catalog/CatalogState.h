#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace cs {

// One resume record (v3 spec §7). `groupKey` is opaque to this store: until
// backend grouping lands (#58+), callers pass the provider-scoped content id;
// once groups exist they pass the "g1:"-prefixed groupKey.
struct ResumeEntry {
    std::string groupKey;
    std::string provider;
    std::string id;               // content id of the chosen source
    std::string episodeId;        // empty for movies
    std::string translationLabel;
    long positionSec = 0;
    long durationSec = 0;
    std::int64_t updatedAt = 0;   // epoch seconds
};

// Remembered source/dub for a series group (movies are never remembered —
// that policy lives at the call site, not in this store).
struct MemoryEntry {
    std::string groupKey;
    std::string provider;
    std::string translationLabel;
    std::int64_t updatedAt = 0;
};

// JSON state store at <dataPath>/catalog_state.json. Writes are atomic
// (tmp + rename). Corrupt/missing files degrade to empty state. Each
// store is LRU-capped; ordering for reads is updatedAt-descended.
class CatalogState {
public:
    static constexpr std::size_t kMaxEntries = 50;

    explicit CatalogState(std::string path) : path_(std::move(path)) {}

    // False on missing or corrupt file (state stays empty). Never throws.
    bool load();

    // Atomic write (path.tmp -> path). False on IO error. Never throws.
    bool save() const;

    // Upsert by groupKey; older entries evicted past kMaxEntries (LRU by updatedAt).
    void setResume(ResumeEntry e);
    void setMemory(MemoryEntry e);

    const ResumeEntry *resume(const std::string &groupKey) const;
    const MemoryEntry *memory(const std::string &groupKey) const;

    // Most recent first, capped.
    std::vector<ResumeEntry> recentResume(std::size_t limit) const;

    // Watched past 95% of a known duration.
    static bool isFinished(const ResumeEntry &e) {
        return e.durationSec > 0 && e.positionSec * 100 >= e.durationSec * 95;
    }

private:
    template <typename T>
    static void upsert(std::vector<T> &v, const T &e) {
        for (auto &cur : v) {
            if (cur.groupKey == e.groupKey) { cur = e; trim(v); return; }
        }
        v.push_back(e);
        trim(v);
    }

    template <typename T>
    static void trim(std::vector<T> &v) {
        while (v.size() > kMaxEntries) {
            auto oldest = v.begin();
            for (auto it = v.begin(); it != v.end(); ++it) {
                if (it->updatedAt < oldest->updatedAt) oldest = it;
            }
            v.erase(oldest);
        }
    }

    std::string path_;
    std::vector<ResumeEntry> resume_;
    std::vector<MemoryEntry> memory_;
};

} // namespace cs
