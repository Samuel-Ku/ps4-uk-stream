#pragma once

#include "main.h"
#include "CatalogApi.h"
#include "OnscreenKeyboard.h"
#include "cross2d/c2d.h"

#include <atomic>
#include <memory>
#include <string>
#include <vector>

namespace cs {

// Full-text search screen.
//
// Layout (top-down):
//   1. Title "Пошук"
//   2. Current text + on-screen keyboard grid
//   3. Status line (Готово / Завантаження… / Помилка)
//   4. Result count
//
// Two-phase input model:
//   - On the keyboard grid, Fire1 appends the focused glyph via
//     OnscreenKeyboard::appendUtf8 (so Cyrillic multi-byte stays intact).
//   - L1 submits; R1 backspaces; Fire2 returns to the main menu.
class ScreenSearch : public c2d::RectangleShape {
public:
    explicit ScreenSearch(c2d::C2DRenderer *main);
    ~ScreenSearch() override = default;

    void onUpdate() override;

private:
    void requestSearch();
    void renderKeyboard();
    void renderStatus();
    void setStatus(const std::string &s);

    CatalogApi *api_ = nullptr;
    Main *main_ = nullptr;
    OnscreenKeyboard kb_;

    c2d::Text *title_ = nullptr;
    c2d::Text *textLabel_ = nullptr;
    c2d::Text *keyboardText_ = nullptr;
    c2d::Text *statusText_ = nullptr;
    c2d::Text *helpText_ = nullptr;
    c2d::RectangleShape *cursor_ = nullptr;

    int kbRow_ = 0;
    int kbCol_ = 0;
    bool inFlight_ = false;
    std::atomic<bool> searchFetched_{false};
    std::vector<SearchItem> fetchedResults_;
    std::string fetchError_;
    size_t resultCount_ = 0;
};

} // namespace cs