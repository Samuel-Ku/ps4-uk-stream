#include "Json.h"
#include "cJSON.h"

namespace cs {

std::string JsonValue::str() const {
    if (node_ == nullptr || !cJSON_IsString(node_) || node_->valuestring == nullptr) return {};
    return std::string(node_->valuestring);
}

std::string JsonValue::str(const std::string &key) const {
    auto *n = cJSON_GetObjectItemCaseSensitive(node_, key.c_str());
    if (n == nullptr || !cJSON_IsString(n) || n->valuestring == nullptr) return {};
    return std::string(n->valuestring);
}

bool JsonValue::has(const std::string &key) const {
    return cJSON_HasObjectItem(node_, key.c_str());
}

int JsonValue::integer(const std::string &key, int fallback) const {
    auto *n = cJSON_GetObjectItemCaseSensitive(node_, key.c_str());
    if (n == nullptr || !cJSON_IsNumber(n)) return fallback;
    return static_cast<int>(n->valuedouble);
}

std::vector<JsonValue> JsonValue::arr(const std::string &key) const {
    std::vector<JsonValue> out;
    auto *n = cJSON_GetObjectItemCaseSensitive(node_, key.c_str());
    if (n == nullptr || !cJSON_IsArray(n)) return out;
    cJSON *child = nullptr;
    cJSON_ArrayForEach(child, n) { out.emplace_back(child); }
    return out;
}

std::vector<JsonValue> JsonValue::asArray() const {
    std::vector<JsonValue> out;
    if (node_ == nullptr || !cJSON_IsArray(node_)) return out;
    cJSON *child = nullptr;
    cJSON_ArrayForEach(child, node_) { out.emplace_back(child); }
    return out;
}

std::vector<std::pair<std::string, JsonValue>> JsonValue::obj() const {
    std::vector<std::pair<std::string, JsonValue>> out;
    if (node_ == nullptr || !cJSON_IsObject(node_)) return out;
    cJSON *child = nullptr;
    cJSON_ArrayForEach(child, node_) {
        if (child->string == nullptr) continue;
        out.emplace_back(std::string(child->string), JsonValue(child));
    }
    return out;
}

std::shared_ptr<JsonDoc> JsonDoc::parse(const std::string &raw) {
    auto *root = cJSON_Parse(raw.c_str());
    if (root == nullptr) return nullptr;
    return std::shared_ptr<JsonDoc>(new JsonDoc(root));
}

JsonDoc::~JsonDoc() {
    if (root_ != nullptr) cJSON_Delete(root_);
}

} // namespace cs
