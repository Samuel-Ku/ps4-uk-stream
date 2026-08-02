#pragma once

#include "main.h"
#include "CatalogApi.h"
#include "ChipStrip.h"
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
//   2. Provider filter chips (one per provider; "all" chip first)
//   3. Current text + on-screen keyboard grid
//   4. Status line (Готово / Завантаження… / Помилка)
//   5. Result count
//
// Two-phase input model:
//   - On the keyboard grid, Fire1 appends the focused glyph via
//     OnscreenKeyboard::appendUtf8 (so Cyrillic multi-byte stays intact).
//   - L1 submits; R1 backspaces; Fire2 returns to the main menu.
//   - Triangle flips a FocusMode into the chip strip (issue #63):
//     L/R selects a provider, Cross refilters, Triangle/Circle returns.
//   - The "all" chip (always first) clears the provider filter.
class ScreenSearch : public c2d::RectangleShape {
public:
    explicit ScreenSearch(c2d::C2DRenderer *main);
    ~ScreenSearch() override;

    void onUpdate() override;

private:
    void requestSearch();
    void renderKeyboard();
    void renderStatus();
    void setStatus(const std::string &s);
    // Remove + delete the previously pushed child screen, if any,
    // and (optionally) install `next` as the new child. Avoids the
    // widget leak when the user re-pushes the same screen type.
    void setChild(c2d::C2DObject *next);
    // Build / rebuild the chip strip from the cached provider roster
    // (populated once on screen entry via sectionsAsync — the search
    // screen owner's delivery channel). The first chip is always the
    // "all" pseudo-provider; the rest come from the live provider list.
    void rebuildChipStrip();
    // Handle an in-flight filter chip pick: refetch with the chosen
    // provider as a query param and re-push the results screen.
    void applyFilter(const std::string &provider);

    enum class FocusMode { Keyboard, Chips };
    FocusMode focusMode_ = FocusMode::Keyboard;
    bool chipStripHasFocus() const { return focusMode_ == FocusMode::Chips; }

    CatalogApi *api_ = nullptr;
    Main *main_ = nullptr;
    c2d::C2DObject *child_ = nullptr; // owned — freed by setChild / dtor
    OnscreenKeyboard kb_;

    c2d::Text *title_ = nullptr;
    c2d::Text *textLabel_ = nullptr;
    c2d::Text *keyboardText_ = nullptr;
    c2d::Text *statusText_ = nullptr;
    c2d::Text *helpText_ = nullptr;
    c2d::RectangleShape *cursor_ = nullptr;
    ui::ChipStrip *chipStrip_ = nullptr;

    // In-flight filter — when non-empty, requestSearch() adds
    // `?provider=<this>` to the search endpoint.
    std::string activeFilter_;

    // Cached provider roster from /api/sections. Populated on screen
    // entry; the chip strip is built from this list. We never call
    // /api/sections again until the user leaves the screen.
    std::vector<ProviderSections> providerRoster_;
    std::atomic<bool> providersFetched_{false};

    int kbRow_ = 0;
    int kbCol_ = 0;
    bool inFlight_ = false;
    std::atomic<bool> searchFetched_{false};
    std::vector<SearchItem> fetchedResults_;
    std::string fetchError_;
    size_t resultCount_ = 0;
};

} // namespace cs