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
