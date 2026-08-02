#include "CatalogState.h"

#include "Json.h"

#include <algorithm>
#include <cstdio>
#include <fstream>
#include <sstream>

#include "cJSON.h"

namespace cs {

namespace {

std::string readFile(const std::string &path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return {};
    std::ostringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

std::vector<ResumeEntry> parseResume(const std::vector<JsonValue> &arr) {
    std::vector<ResumeEntry> out;
    for (const auto &v : arr) {
        ResumeEntry e;
        e.groupKey = v.str("group");
        e.provider = v.str("provider");
        e.id = v.str("id");
        e.episodeId = v.str("episode");
        e.translationLabel = v.str("translation");
        e.positionSec = v.integer("pos", 0);
        e.durationSec = v.integer("dur", 0);
        e.updatedAt = static_cast<std::int64_t>(v.integer("at", 0));
        if (!e.groupKey.empty()) out.push_back(std::move(e));
    }
    return out;
}

std::vector<MemoryEntry> parseMemory(const std::vector<JsonValue> &arr) {
    std::vector<MemoryEntry> out;
    for (const auto &v : arr) {
        MemoryEntry e;
        e.groupKey = v.str("group");
        e.provider = v.str("provider");
        e.translationLabel = v.str("translation");
        e.updatedAt = static_cast<std::int64_t>(v.integer("at", 0));
        if (!e.groupKey.empty()) out.push_back(std::move(e));
    }
    return out;
}

// Writes go through cJSON directly: cs::Json is a read-only wrapper.
std::string serialize(const std::vector<ResumeEntry> &resume,
                      const std::vector<MemoryEntry> &memory) {
    cJSON *root = cJSON_CreateObject();

    cJSON *resumeArr = cJSON_AddArrayToObject(root, "resume");
    for (const auto &e : resume) {
        cJSON *o = cJSON_CreateObject();
        cJSON_AddStringToObject(o, "group", e.groupKey.c_str());
        cJSON_AddStringToObject(o, "provider", e.provider.c_str());
        cJSON_AddStringToObject(o, "id", e.id.c_str());
        cJSON_AddStringToObject(o, "episode", e.episodeId.c_str());
        cJSON_AddStringToObject(o, "translation", e.translationLabel.c_str());
        cJSON_AddNumberToObject(o, "pos", static_cast<double>(e.positionSec));
        cJSON_AddNumberToObject(o, "dur", static_cast<double>(e.durationSec));
        cJSON_AddNumberToObject(o, "at", static_cast<double>(e.updatedAt));
        cJSON_AddItemToArray(resumeArr, o);
    }

    cJSON *memoryArr = cJSON_AddArrayToObject(root, "memory");
    for (const auto &e : memory) {
        cJSON *o = cJSON_CreateObject();
        cJSON_AddStringToObject(o, "group", e.groupKey.c_str());
        cJSON_AddStringToObject(o, "provider", e.provider.c_str());
        cJSON_AddStringToObject(o, "translation", e.translationLabel.c_str());
        cJSON_AddNumberToObject(o, "at", static_cast<double>(e.updatedAt));
        cJSON_AddItemToArray(memoryArr, o);
    }

    char *raw = cJSON_PrintUnformatted(root);
    std::string out = raw != nullptr ? raw : "{}";
    cJSON_free(raw);
    cJSON_Delete(root);
    return out;
}

} // namespace

bool CatalogState::load() {
    const std::string raw = readFile(path_);
    if (raw.empty()) return false;
    auto doc = JsonDoc::parse(raw);
    if (!doc) return false;
    auto root = doc->root();
    resume_ = parseResume(root.arr("resume"));
    memory_ = parseMemory(root.arr("memory"));
    return true;
}

bool CatalogState::save() const {
    const std::string tmp = path_ + ".tmp";
    {
        std::ofstream f(tmp, std::ios::binary | std::ios::trunc);
        if (!f) return false;
        const std::string raw = serialize(resume_, memory_);
        f.write(raw.data(), static_cast<std::streamsize>(raw.size()));
        if (!f.good()) {
            f.close();
            std::remove(tmp.c_str());
            return false;
        }
    }
    if (std::rename(tmp.c_str(), path_.c_str()) != 0) {
        std::remove(tmp.c_str());
        return false;
    }
    return true;
}

void CatalogState::setResume(ResumeEntry e) {
    upsert(resume_, e);
}

void CatalogState::setMemory(MemoryEntry e) {
    upsert(memory_, e);
}

const ResumeEntry *CatalogState::resume(const std::string &groupKey) const {
    for (const auto &e : resume_) {
        if (e.groupKey == groupKey) return &e;
    }
    return nullptr;
}

const MemoryEntry *CatalogState::memory(const std::string &groupKey) const {
    for (const auto &e : memory_) {
        if (e.groupKey == groupKey) return &e;
    }
    return nullptr;
}

std::vector<ResumeEntry> CatalogState::recentResume(std::size_t limit) const {
    std::vector<ResumeEntry> out = resume_;
    std::stable_sort(out.begin(), out.end(),
                     [](const ResumeEntry &a, const ResumeEntry &b) {
                         return a.updatedAt > b.updatedAt;
                     });
    if (out.size() > limit) out.resize(limit);
    return out;
}

} // namespace cs
