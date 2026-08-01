#include "OnscreenKeyboard.h"
#include <cassert>
#include <cstdint>

namespace cs {

namespace {
const char *kLayout[OnscreenKeyboard::kRows][OnscreenKeyboard::kCols] = {
    {"А","Б","В","Г","Ґ","Д","Е","Є","Ж","З"},
    {"И","І","Ї","Й","К","Л","М","Н","О","П"},
    {"Р","С","Т","У","Ф","Х","Ц","Ч","Ш","Щ"},
    {"Ь","Ю","Я","0","1","2","3","4","5","6"},
    {"7","8","9","space","back","clear","done","","",""}
};
} // namespace

OnscreenKeyboard::OnscreenKeyboard() {
    grid_.reserve(kRows * kCols);
    for (int r = 0; r < kRows; ++r) {
        for (int c = 0; c < kCols; ++c) {
            grid_.emplace_back(kLayout[r][c]);
        }
    }
}

const std::string &OnscreenKeyboard::labelAt(int row, int col) const {
    assert(row >= 0 && row < kRows && col >= 0 && col < kCols);
    return grid_[row * kCols + col];
}

void OnscreenKeyboard::setText(std::string t) { text_ = std::move(t); }

void OnscreenKeyboard::append(char32_t cp) {
    char buf[5] = {0};
    if (cp < 0x80) {
        buf[0] = static_cast<char>(cp);
    } else if (cp < 0x800) {
        buf[0] = static_cast<char>(0xC0 | (cp >> 6));
        buf[1] = static_cast<char>(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
        buf[0] = static_cast<char>(0xE0 | (cp >> 12));
        buf[1] = static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        buf[2] = static_cast<char>(0x80 | (cp & 0x3F));
    } else {
        buf[0] = static_cast<char>(0xF0 | (cp >> 18));
        buf[1] = static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
        buf[2] = static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        buf[3] = static_cast<char>(0x80 | (cp & 0x3F));
    }
    text_ += buf;
}

void OnscreenKeyboard::appendUtf8(const std::string &utf8) {
    // Decode the first UTF-8 codepoint and append it. Returns silently
    // on empty input; on malformed input we fall back to the raw byte
    // (preserves the old append(label[0]) behaviour as a safety net).
    if (utf8.empty()) return;
    const auto *p = reinterpret_cast<const unsigned char *>(utf8.data());
    char32_t cp = 0;
    int len = 0;
    if (p[0] < 0x80) {
        cp = p[0];
        len = 1;
    } else if ((p[0] & 0xE0) == 0xC0) {
        cp = (static_cast<char32_t>(p[0] & 0x1F) << 6) | (p[1] & 0x3F);
        len = 2;
    } else if ((p[0] & 0xF0) == 0xE0) {
        cp = (static_cast<char32_t>(p[0] & 0x0F) << 12) |
             (static_cast<char32_t>(p[1] & 0x3F) << 6) |
             (p[2] & 0x3F);
        len = 3;
    } else if ((p[0] & 0xF8) == 0xF0) {
        cp = (static_cast<char32_t>(p[0] & 0x07) << 18) |
             (static_cast<char32_t>(p[1] & 0x3F) << 12) |
             (static_cast<char32_t>(p[2] & 0x3F) << 6) |
             (p[3] & 0x3F);
        len = 4;
    } else {
        // Malformed: behave like the old buggy code (append first byte).
        cp = p[0];
        len = 1;
    }
    append(cp);
    (void)len;  // length available if we want to truncate later
}

void OnscreenKeyboard::backspace() {
    if (text_.empty()) return;
    size_t i = text_.size() - 1;
    while (i > 0 && (static_cast<unsigned char>(text_[i]) & 0xC0) == 0x80) --i;
    text_.erase(i);
}

void OnscreenKeyboard::clear() { text_.clear(); }

bool OnscreenKeyboard::isAction(int row, int col, std::string &action) const {
    if (row != kRows - 1) return false;
    const auto &l = labelAt(row, col);
    if (l == "space" || l == "back" || l == "clear" || l == "done") {
        action = l;
        return true;
    }
    return false;
}

} // namespace cs
