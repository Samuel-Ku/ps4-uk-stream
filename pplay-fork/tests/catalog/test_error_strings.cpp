// Tests for the error-code → human-UA-string mapping (issue #64, v3 spec §5.4).
//
// Seam under test: cs::ui::humanError — every catalog error surface
// routes through this function; raw snake_case codes from the backend
// must never reach the user.

#include "ErrorStrings.h"

#include <cstdio>
#include <string>

namespace {

int g_passed = 0;
int g_failed = 0;

#define CHECK_EQ(a, b) do { \
    auto _a = std::string(a); auto _b = std::string(b); \
    if (_a == _b) { ++g_passed; } \
    else { ++g_failed; std::fprintf(stderr, "FAIL %s:%d: %s == %s  (got '%s' vs '%s')\n", \
        __FILE__, __LINE__, #a, #b, _a.c_str(), _b.c_str()); } \
} while (0)

#define CHECK(cond) do { \
    if (cond) { ++g_passed; } \
    else { ++g_failed; std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } \
} while (0)

} // namespace

int main() {
    using cs::ui::humanError;
    using cs::ui::humanErrorOrGeneric;

    // Backend wire codes (ADR-0002 failure envelope + v3 spec §2.1.3) →
    // human UA strings.
    CHECK_EQ(humanError("upstream_unreachable"), "Джерело тимчасово недоступне");
    CHECK_EQ(humanError("502"),                   "Джерело тимчасово недоступне");
    CHECK_EQ(humanError("search_timeout"),        "Сервер недоступний");
    CHECK_EQ(humanError("timeout"),               "Сервер тимчасово не відповідає");
    CHECK_EQ(humanError("translation_missing"),   "Цей переклад недоступний");
    CHECK_EQ(humanError("invalid_translation"),   "Цей переклад недоступний");
    CHECK_EQ(humanError("not_found"),             "Контент не знайдено");
    CHECK_EQ(humanError("empty_results"),         "Нічого не знайдено");
    CHECK_EQ(humanError("internal"),              "Сталася помилка");

    // Unknown code → generic fallback. Raw snake_case must NOT leak.
    CHECK_EQ(humanError("some_unmapped_code"), "Сталася помилка");
    CHECK_EQ(humanError(""),                   "Сталася помилка");

    // humanErrorOrGeneric: empty input collapses to generic too.
    CHECK_EQ(humanErrorOrGeneric(""), "Сталася помилка");
    CHECK_EQ(humanErrorOrGeneric("upstream_unreachable"), "Джерело тимчасово недоступне");

    // No string in the table contains an underscore or a colon — the
    // regression guard against raw code leakage.
    auto isHumanUa = [](const std::string &s) {
        for (char c : s) {
            if (c == '_') return false;
        }
        return true;
    };
    CHECK(isHumanUa(humanError("upstream_unreachable")));
    CHECK(isHumanUa(humanError("translation_missing")));
    CHECK(isHumanUa(humanError("search_timeout")));

    std::printf("error_strings: %d passed, %d failed\n", g_passed, g_failed);
    return g_failed == 0 ? 0 : 1;
}
