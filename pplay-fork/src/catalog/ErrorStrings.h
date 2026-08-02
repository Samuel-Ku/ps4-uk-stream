#pragma once

// Error-code → human Ukrainian string mapping (issue #64, v3 spec §5.4).
//
// The catalog shows the mapped UA string at every error surface; raw codes
// from the backend (snake_case) are never surfaced. New codes land here in
// one place; the rest of the catalog calls `humanError(code)` and renders
// the result verbatim.
//
// Codes are intentionally the backend's wire vocabulary (issue #80 ADR-0002
// failure envelope + v3 spec §2.1.3). Adding a new code is a single switch
// case below — call sites stay unchanged.

#include <string>

namespace cs::ui {

// Backend wire codes → human Ukrainian strings. Empty string means
// "unknown" — `humanError` falls back to a generic "Сталася помилка" for
// any code we don't recognise. New codes are added here in the order they
// appear in the backend.
inline std::string humanError(const std::string &code) {
    if (code == "upstream_unreachable") return "Джерело тимчасово недоступне";
    if (code == "502")                   return "Джерело тимчасово недоступне";
    if (code == "search_timeout")        return "Сервер недоступний";
    if (code == "timeout")               return "Сервер тимчасово не відповідає";
    if (code == "translation_missing")   return "Цей переклад недоступний";
    if (code == "invalid_translation")   return "Цей переклад недоступний";
    if (code == "not_found")             return "Контент не знайдено";
    if (code == "empty_results")         return "Нічого не знайдено";
    if (code == "internal")              return "Сталася помилка";
    return "Сталася помилка";
}

// Convenience: empty / generic backend-error string → generic UA.
inline std::string humanErrorOrGeneric(const std::string &code) {
    if (code.empty()) return "Сталася помилка";
    return humanError(code);
}

} // namespace cs::ui
