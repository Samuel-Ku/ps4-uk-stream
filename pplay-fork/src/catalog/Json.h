#pragma once

#include <memory>
#include <string>
#include <vector>

struct cJSON;

namespace cs {

class JsonValue {
public:
    explicit JsonValue(cJSON *node) : node_(node) {}
    std::string str(const std::string &key) const;
    bool has(const std::string &key) const;
    int integer(const std::string &key, int fallback = 0) const;
    std::vector<JsonValue> arr(const std::string &key) const;
    std::vector<JsonValue> asArray() const;
private:
    cJSON *node_;
};

class JsonDoc {
public:
    static std::shared_ptr<JsonDoc> parse(const std::string &raw);
    ~JsonDoc();
    JsonValue root() const { return JsonValue(root_); }
private:
    explicit JsonDoc(cJSON *root) : root_(root) {}
    cJSON *root_;
};

} // namespace cs
