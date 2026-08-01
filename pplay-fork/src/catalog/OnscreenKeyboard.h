#pragma once

#include <string>
#include <vector>

namespace cs {

class OnscreenKeyboard {
public:
    static constexpr int kRows = 5;
    static constexpr int kCols = 10;

    OnscreenKeyboard();

    const std::string &labelAt(int row, int col) const;

    void setText(std::string t);
    const std::string &text() const { return text_; }
    void append(char32_t cp);
    // Decode one UTF-8 codepoint from ``utf8`` (may be 1-4 bytes) and
    // append it to the buffer. Used by ScreenSearch, which gets the
    // label as a UTF-8 std::string from labelAt() rather than a
    // single codepoint. Without this, taking ``label[0]`` would
    // append only the first byte of a multi-byte Cyrillic letter.
    void appendUtf8(const std::string &utf8);
    void backspace();
    void clear();

    int rows() const { return kRows; }
    int cols() const { return kCols; }

    bool isAction(int row, int col, std::string &action) const;

private:
    std::vector<std::string> grid_;
    std::string text_;
};

} // namespace cs
