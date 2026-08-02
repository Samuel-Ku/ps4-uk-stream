#pragma once

// ScreenHome — single catalog entry point (issue #61, v3 spec §3.1).
//
// One screen that owns all five row types from /api/home:
//   - «Новинки»
//   - conditional «Популярні зараз»
//   - five conditional type rows: «Фільми», «Серіали», «Аніме»,
//     «Мультфільми», «Дорами»
//
// Layout: vertical stack of rows. Each row has a header label, a
// horizontal strip of cards (up to 20), and an optional «Ще» tail slot
// that opens the browse screen for the row's type. The header has a
// search loupe — pressing Cross on it opens ScreenSearch.
//
// Backend unreachable on entry renders an inline error screen with
// «Повторити»; no background retries, no freeze. Other pPlay menus are
// unaffected — this screen owns only its own children.

#include "main.h"
#include "CatalogApi.h"
#include "cross2d/c2d.h"

#include <atomic>
#include <string>
#include <vector>

namespace cs {

class ScreenHome : public c2d::RectangleShape {
public:
    explicit ScreenHome(c2d::C2DRenderer *main);
    ~ScreenHome() override;

    void onUpdate() override;

private:
    enum class LoadState { Idle, Loading, Loaded, Failed };
    // Focusable rows: loupe, then each row (type row → its cards are
    // navigated with Left/Right; Up/Down moves to the next row or back
    // to the loupe).
    enum class FocusRow { Loupe, Rows };

    // Build the static chrome (background, loupe, error-screen widgets,
    // help text). Done once in the constructor.
    void buildChrome();
    // Fetch /api/home. The worker-thread callback marshals into
    // fetchedRows_ + sets homeFetched_; onUpdate() drains it once.
    void requestHome();
    // Tear down the per-row card widgets and rebuild them from
    // rows_. Called when the response lands.
    void rebuildRows();
    // Re-position all row widgets to match the current scroll offset.
    // The screen scrolls vertically when the focused row would push
    // past the bottom edge.
    void layoutRows();
    // Move focus in `dir` (-1 up, +1 down) over the rows + loupe.
    void moveFocusVertical(int dir);
    // Move card focus within the current row (Left/Right). Skips the
    // «Ще» slot when the row has fewer items than the cap and skips
    // disabled rows. Returns true if focus changed.
    bool moveFocusHorizontal(int dir);
    // Push the appropriate ScreenResults for the focused row's tail
    // slot, or open ScreenContent for the focused card.
    void activateFocused();
    // Push ScreenSearch, replacing any prior search child.
    void openSearch();
    // Drop any previously pushed child screen, install `next`. Used by
    // both content and search navigation so the children list stays
    // bounded.
    void setChild(c2d::C2DObject *next);
    // Set the visible error message; toggles error-screen visibility.
    void showError(const std::string &msg);
    // Re-fetch /api/home (the «Повторити» action).
    void retry();

    CatalogApi *api_ = nullptr;
    Main *main_ = nullptr;
    c2d::C2DObject *child_ = nullptr;

    // Background fill + title chrome.
    c2d::Text *title_ = nullptr;
    // Search loupe at the top: a focusable pill labeled "Пошук".
    c2d::RectangleShape *loupeBox_ = nullptr;
    c2d::Text *loupeLabel_ = nullptr;
    c2d::RectangleShape *cursor_ = nullptr;

    // Help / status line at the bottom.
    c2d::Text *helpText_ = nullptr;
    c2d::Text *statusText_ = nullptr;

    // Error-screen chrome (created up-front; toggled via visibility).
    c2d::Text *errorTitle_ = nullptr;
    c2d::Text *errorBody_ = nullptr;
    c2d::RectangleShape *errorRetryBox_ = nullptr;
    c2d::Text *errorRetryLabel_ = nullptr;

    // Rows. Each row owns one label + N card boxes + N card labels +
    // (optionally) a «Ще» pill. The lifetime is managed here: rebuild
    // frees the previous row's widgets before installing new ones.
    struct RowView {
        HomeRow data;
        c2d::Text *header = nullptr;
        std::vector<c2d::RectangleShape *> cards;
        std::vector<c2d::Text *> titles;
        c2d::RectangleShape *moreBox = nullptr;
        c2d::Text *moreLabel = nullptr;
        float y = 0.0f;
        float height = 0.0f;
    };
    std::vector<RowView> rows_;
    int focusedRowIndex_ = 0;       // index into rows_, or -1 = loupe
    int focusedCardIndex_ = 0;      // index into rows_[i].cards, last = «Ще»
    FocusRow focusRow_ = FocusRow::Loupe;
    // Vertical scroll offset (pixels). The screen is `main_->getSize().y`
    // tall; when the focused row would push past the bottom, we scroll.
    float scrollOffset_ = 0.0f;

    // Worker-thread handoff.
    std::atomic<bool> homeFetched_{false};
    HomeResponse fetchedResp_;
    std::string fetchError_;
    LoadState loadState_ = LoadState::Idle;
};

} // namespace cs
