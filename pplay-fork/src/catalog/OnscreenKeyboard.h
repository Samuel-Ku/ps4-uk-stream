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
