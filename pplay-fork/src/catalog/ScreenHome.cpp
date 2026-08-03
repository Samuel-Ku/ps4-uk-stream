#include "UiScale.h"
#include "ScreenHome.h"
#include "CatalogContext.h"
#include "ErrorStrings.h"
#include "main.h"
#include "ScreenSearch.h"
#include "ScreenResults.h"
#include "ScreenContent.h"

#include <cstdio>
#include <sstream>
#include <unordered_map>

namespace cs {

namespace {
using ui::kSmallSize;
using ui::kBodySize;
using ui::kTitleSize;
using ui::kMarginX;
using ui::kMarginY;
using ui::kGap;
using ui::kFocusOutline;
using ui::drawFocusBox;

// 10-foot layout constants (issue #57). Anchored to 1080p.
constexpr float kRowGap = 24.0f;            // vertical gap between rows
constexpr float kRowHeaderHeight = 40.0f;   // row label baseline height
constexpr float kCardWidth = 192.0f;        // 16:9 thumbnail at 1080p
constexpr float kCardHeight = 108.0f;       // 16:9 thumbnail height
constexpr float kCardGap = 16.0f;           // horizontal gap between cards
constexpr float kMoreWidth = 96.0f;         // «Ще» pill width
constexpr float kBottomReserve = 96.0f;      // reserve for help/status text

// Card focus cycles 0..N (cards), with N = "more" (the «Ще» slot).
inline int cardLastIndex(const cs::HomeRow &r) {
    // 0..(items-1) are the cards; items.size() == the «Ще» slot index.
    return static_cast<int>(r.items.size());
}

} // namespace

ScreenHome::ScreenHome(c2d::C2DRenderer *main)
    : RectangleShape({0, 0,
                      static_cast<float>(static_cast<c2d::C2DRenderer *>(main)->getSize().x),
                      static_cast<float>(static_cast<c2d::C2DRenderer *>(main)->getSize().y)}),
      api_(CatalogContext::get()),
      main_(static_cast<Main *>(main)) {
    setFillColor(c2d::Color{0x12, 0x12, 0x12, 0xff});
    setLayer(5);

    buildChrome();
    requestHome();
}

ScreenHome::~ScreenHome() {
    if (child_ != nullptr) {
        if (main_ != nullptr) main_->remove(child_);
        delete child_;
        child_ = nullptr;
    }
    // Free per-row widgets (cards, titles, headers, «Ще» pills). The
    // parent (this RectangleShape) holds them as children; deleteing
    // here keeps the order explicit even though DeleteMode::Auto would
    // also clean them up.
    for (auto &r : rows_) {
        if (r.header) {
            remove(r.header);
            delete r.header;
        }
        for (auto *c : r.cards) { remove(c); delete c; }
        for (auto *t : r.titles) { remove(t); delete t; }
        if (r.moreBox) { remove(r.moreBox); delete r.moreBox; }
        if (r.moreLabel) { remove(r.moreLabel); delete r.moreLabel; }
    }
    rows_.clear();
}

void ScreenHome::buildChrome() {
    const float W = static_cast<float>(main_->getSize().x);
    const float H = static_cast<float>(main_->getSize().y);

    title_ = new c2d::Text("Каталог UA", kTitleSize, main_->getFont());
    title_->setPosition({kMarginX, kMarginY});
    title_->setFillColor(c2d::Color{0xff, 0xff, 0xff, 0xff});
    add(title_);

    // Loupe pill at the top-right. Width is a fixed slice so it sits
    // flush against the right margin.
    const float loupeW = 320.0f;
    const float loupeH = kBodySize + 16.0f;
    const float loupeX = W - kMarginX - loupeW;
    const float loupeY = kMarginY - 4.0f;
    loupeBox_ = new c2d::RectangleShape({loupeX, loupeY, loupeW, loupeH});
    loupeBox_->setFillColor(c2d::Color{0x12, 0x12, 0x12, 0xff});
    loupeBox_->setOutlineColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
    loupeBox_->setOutlineThickness(2.0f);
    add(loupeBox_);

    loupeLabel_ = new c2d::Text("🔍 Пошук", kBodySize, main_->getFont());
    loupeLabel_->setPosition({loupeX + 16.0f, loupeY + 8.0f});
    loupeLabel_->setFillColor(c2d::Color{0xff, 0xff, 0xff, 0xff});
    add(loupeLabel_);

    cursor_ = new c2d::RectangleShape({0, 0, 0, 0});
    cursor_->setFillColor(c2d::Color{0x55, 0xef, 0xc4, 0x40});
    cursor_->setOutlineColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
    cursor_->setOutlineThickness(kFocusOutline);
    add(cursor_);

    statusText_ = new c2d::Text("Завантаження…", kSmallSize, main_->getFont());
    statusText_->setPosition({kMarginX, H - 2 * (kSmallSize + 8) - kMarginY});
    statusText_->setFillColor(c2d::Color{0xaa, 0xaa, 0xaa, 0xff});
    add(statusText_);

    helpText_ = new c2d::Text("X: вибрати · ←↑↓→: навігація · O: назад",
                              kSmallSize, main_->getFont());
    helpText_->setPosition({kMarginX, H - (kSmallSize + 8) - kMarginY});
    helpText_->setFillColor(c2d::Color{0x88, 0x88, 0x88, 0xff});
    add(helpText_);

    // Error-screen chrome — created hidden. We toggle visibility
    // when the backend reports a failure (no auto-retry).
    errorTitle_ = new c2d::Text("Сервер недоступний", kTitleSize, main_->getFont());
    errorTitle_->setPosition({kMarginX, H / 2.0f - 80.0f});
    errorTitle_->setFillColor(c2d::Color{0xff, 0xaa, 0xaa, 0xff});
    errorTitle_->setVisibility(c2d::Visibility::Hidden);
    add(errorTitle_);

    errorBody_ = new c2d::Text(
        "Перевірте, чи увімкнено ПК,\nта Налаштування → Адреса сервера.",
        kBodySize, main_->getFont());
    errorBody_->setPosition({kMarginX, H / 2.0f - 20.0f});
    errorBody_->setFillColor(c2d::Color{0xee, 0xee, 0xee, 0xff});
    errorBody_->setVisibility(c2d::Visibility::Hidden);
    add(errorBody_);

    const float retryW = 240.0f;
    const float retryH = kBodySize + 24.0f;
    errorRetryBox_ = new c2d::RectangleShape(
        {W / 2.0f - retryW / 2.0f, H / 2.0f + 60.0f, retryW, retryH});
    errorRetryBox_->setFillColor(c2d::Color{0x12, 0x12, 0x12, 0xff});
    errorRetryBox_->setOutlineColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
    errorRetryBox_->setOutlineThickness(kFocusOutline);
    errorRetryBox_->setVisibility(c2d::Visibility::Hidden);
    add(errorRetryBox_);

    errorRetryLabel_ = new c2d::Text("Повторити", kBodySize, main_->getFont());
    errorRetryLabel_->setPosition(
        {W / 2.0f - 60.0f, H / 2.0f + 60.0f + 12.0f});
    errorRetryLabel_->setFillColor(c2d::Color{0xff, 0xff, 0xff, 0xff});
    errorRetryLabel_->setVisibility(c2d::Visibility::Hidden);
    add(errorRetryLabel_);
}

void ScreenHome::requestHome() {
    if (!api_) {
        showError("Backend недоступний");
        return;
    }
    loadState_ = LoadState::Loading;
    homeFetched_.store(false, std::memory_order_release);
    api_->homeAsync(
        [this](bool ok, HomeResponse resp, std::string err) {
            if (ok) {
                fetchedResp_ = std::move(resp);
                fetchError_.clear();
            } else {
                fetchError_ = cs::ui::humanErrorOrGeneric(err);
            }
            homeFetched_.store(true, std::memory_order_release);
        });
}

void ScreenHome::rebuildRows() {
    // Drop any prior row widgets — the new response may have a
    // different shape (e.g. «Популярні зараз» appeared or vanished).
    for (auto &r : rows_) {
        if (r.header) { remove(r.header); delete r.header; }
        for (auto *c : r.cards) { remove(c); delete c; }
        for (auto *t : r.titles) { remove(t); delete t; }
        if (r.moreBox) { remove(r.moreBox); delete r.moreBox; }
        if (r.moreLabel) { remove(r.moreLabel); delete r.moreLabel; }
    }
    rows_.clear();

    // Issue #66 — prepend the «Продовжити перегляд» row built from
    // the local state store. It sits above the backend rows because
    // resume is the user's primary intent after a return visit.
    prependResumeRow();

    // Build one RowView per backend row. Header + cards + «Ще» pill.
    for (const auto &src : fetchedResp_.rows) {
        RowView v;
        v.kind = "home";
        v.data = src;
        v.header = new c2d::Text(src.title, kBodySize, main_->getFont());
        v.header->setFillColor(c2d::Color{0xff, 0xff, 0xff, 0xff});
        add(v.header);

        for (const auto &it : src.items) {
            auto *box = new c2d::RectangleShape({0, 0, kCardWidth, kCardHeight});
            box->setFillColor(c2d::Color{0x22, 0x22, 0x22, 0xff});
            box->setOutlineColor(c2d::Color{0x55, 0x55, 0x55, 0xff});
            box->setOutlineThickness(1.0f);
            add(box);
            v.cards.push_back(box);

            // Truncate long titles to fit the card width — 24 chars is a
            // safe ceiling for kCardWidth at kSmallSize.
            std::string t = it.title;
            if (t.size() > 28) t = t.substr(0, 27) + "…";
            auto *label = new c2d::Text(t, kSmallSize, main_->getFont());
            label->setFillColor(c2d::Color{0xee, 0xee, 0xee, 0xff});
            add(label);
            v.titles.push_back(label);
        }
        // «Ще» pill (conditional: skip on the «Новинки» row, where
        // "more" has no obvious meaning — newest is the full feed).
        if (src.type != "newest") {
            v.moreBox = new c2d::RectangleShape({0, 0, kMoreWidth, kCardHeight});
            v.moreBox->setFillColor(c2d::Color{0x12, 0x12, 0x12, 0xff});
            v.moreBox->setOutlineColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
            v.moreBox->setOutlineThickness(2.0f);
            add(v.moreBox);
            v.moreLabel = new c2d::Text("Ще →", kBodySize, main_->getFont());
            v.moreLabel->setFillColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
            add(v.moreLabel);
        }
        rows_.push_back(std::move(v));
    }
    // Reset focus to the first row / first card.
    focusedRowIndex_ = rows_.empty() ? -1 : 0;
    focusedCardIndex_ = 0;
    focusRow_ = rows_.empty() ? FocusRow::Loupe : FocusRow::Rows;
    scrollOffset_ = 0.0f;
    layoutRows();
}

void ScreenHome::prependResumeRow() {
    auto *state = CatalogContext::state();
    if (state == nullptr) return;
    const auto recent = state->recentResume(kResumeLimit);
    // Filter finished entries (>= 95% per CatalogState::isFinished).
    // On a "fresh start" the user gets the home row again, NOT a
    // re-offered position at the end.
    std::vector<ResumeEntry> live;
    live.reserve(recent.size());
    for (const auto &e : recent) {
        if (CatalogState::isFinished(e)) continue;
        live.push_back(e);
    }
    if (live.empty()) return;
    // Build one card per live entry. The resume store doesn't carry
    // a title or poster (those live on the backend); we synthesize a
    // short placeholder from the resume provider + position so the
    // user sees SOMETHING identifying the row. Activation routes via
    // groupKey → ScreenContent, which fetches the real metadata.
    RowView v;
    v.kind = "resume";
    v.header = new c2d::Text("Продовжити перегляд", kBodySize, main_->getFont());
    v.header->setFillColor(c2d::Color{0xff, 0xff, 0xff, 0xff});
    add(v.header);
    for (const auto &e : live) {
        auto *box = new c2d::RectangleShape({0, 0, kCardWidth, kCardHeight});
        box->setFillColor(c2d::Color{0x22, 0x22, 0x22, 0xff});
        box->setOutlineColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
        box->setOutlineThickness(2.0f);
        add(box);
        v.cards.push_back(box);
        // Render: "prov · NN:NN" so the user sees source + position.
        std::string label = e.provider;
        const long mins = e.positionSec / 60;
        const long secs = e.positionSec % 60;
        char buf[16];
        std::snprintf(buf, sizeof(buf), " · %02ld:%02ld", mins, secs);
        label += buf;
        if (label.size() > 28) label = label.substr(0, 27) + "…";
        auto *t = new c2d::Text(label, kSmallSize, main_->getFont());
        t->setFillColor(c2d::Color{0xee, 0xee, 0xee, 0xff});
        add(t);
        v.titles.push_back(t);
        v.resumeEntries.push_back(e);
    }
    // The resume row NEVER carries a «Ще» pill — there's no
    // "browse all resume entries" surface.
    v.moreBox = nullptr;
    v.moreLabel = nullptr;
    rows_.push_back(std::move(v));
}

void ScreenHome::layoutRows() {
    if (rows_.empty()) {
        cursor_->setVisibility(c2d::Visibility::Hidden);
        return;
    }
    const float W = static_cast<float>(main_->getSize().x);
    const float H = static_cast<float>(main_->getSize().y);
    const float rowsAreaTop = kMarginY + kTitleSize + 24.0f;
    const float rowsAreaBottom = H - kBottomReserve;

    // Vertical stack. Each row consumes: header + kRowGap + card row.
    float y = rowsAreaTop - scrollOffset_;
    for (auto &r : rows_) {
        r.y = y;
        r.height = kRowHeaderHeight + kRowGap + kCardHeight;
        r.header->setPosition({kMarginX, y});
        y += kRowHeaderHeight + kRowGap;

        float x = kMarginX;
        for (size_t i = 0; i < r.cards.size(); ++i) {
            r.cards[i]->setPosition({x, y});
            r.titles[i]->setPosition({x, y + kCardHeight + 4.0f});
            x += kCardWidth + kCardGap;
        }
        if (r.moreBox != nullptr) {
            r.moreBox->setPosition({x, y});
            r.moreLabel->setPosition({x + 12.0f, y + (kCardHeight - kBodySize) / 2.0f});
        }
        y += kCardHeight + kRowGap;
    }

    // Reposition the focus cursor on the focused element. The cursor
    // is shared between the loupe, individual cards, and the «Ще» pill.
    if (focusRow_ == FocusRow::Loupe) {
        drawFocusBox(cursor_,
                     {loupeBox_->getPosition().x - 4.0f,
                      loupeBox_->getPosition().y - 4.0f,
                      loupeBox_->getSize().x + 8.0f,
                      loupeBox_->getSize().y + 8.0f},
                     kFocusOutline);
        cursor_->setVisibility(c2d::Visibility::Visible);
        return;
    }
    if (focusedRowIndex_ < 0 || focusedRowIndex_ >= static_cast<int>(rows_.size())) {
        cursor_->setVisibility(c2d::Visibility::Hidden);
        return;
    }
    const auto &row = rows_[focusedRowIndex_];
    if (focusedCardIndex_ == cardLastIndex(row.data) && row.moreBox != nullptr) {
        const float padding = 4.0f;
        drawFocusBox(cursor_,
                     {row.moreBox->getPosition().x - padding,
                      row.moreBox->getPosition().y - padding,
                      row.moreBox->getSize().x + 2.0f * padding,
                      row.moreBox->getSize().y + 2.0f * padding},
                     kFocusOutline);
    } else if (focusedCardIndex_ >= 0 &&
               focusedCardIndex_ < static_cast<int>(row.cards.size())) {
        const float padding = 4.0f;
        const auto &box = row.cards[focusedCardIndex_];
        drawFocusBox(cursor_,
                     {box->getPosition().x - padding,
                      box->getPosition().y - padding,
                      box->getSize().x + 2.0f * padding,
                      box->getSize().y + 2.0f * padding},
                     kFocusOutline);
    } else {
        cursor_->setVisibility(c2d::Visibility::Hidden);
        return;
    }
    cursor_->setVisibility(c2d::Visibility::Visible);
    (void)W;
}

void ScreenHome::moveFocusVertical(int dir) {
    if (rows_.empty()) return;
    if (focusRow_ == FocusRow::Loupe) {
        if (dir > 0) {
            focusRow_ = FocusRow::Rows;
            focusedRowIndex_ = 0;
            focusedCardIndex_ = 0;
        }
        return;
    }
    // FocusRow::Rows
    int next = focusedRowIndex_ + dir;
    if (next < 0) {
        focusRow_ = FocusRow::Loupe;
        focusedRowIndex_ = 0;
        return;
    }
    if (next >= static_cast<int>(rows_.size())) return;
    focusedRowIndex_ = next;
    focusedCardIndex_ = 0;
}

bool ScreenHome::moveFocusHorizontal(int dir) {
    if (rows_.empty() || focusRow_ != FocusRow::Rows) return false;
    if (focusedRowIndex_ < 0 || focusedRowIndex_ >= static_cast<int>(rows_.size())) return false;
    const auto &row = rows_[focusedRowIndex_];
    int last = cardLastIndex(row.data);
    // «Ще» only exists for non-newest rows. On «Новинки» we cycle 0..N-1.
    if (row.moreBox == nullptr) last = static_cast<int>(row.cards.size()) - 1;
    int next = focusedCardIndex_ + dir;
    if (next < 0) next = last;
    if (next > last) next = 0;
    if (next == focusedCardIndex_) return false;
    focusedCardIndex_ = next;
    return true;
}

void ScreenHome::activateFocused() {
    if (focusRow_ == FocusRow::Loupe) {
        openSearch();
        return;
    }
    if (focusedRowIndex_ < 0 || focusedRowIndex_ >= static_cast<int>(rows_.size())) return;
    const auto &row = rows_[focusedRowIndex_];
    const int last = cardLastIndex(row.data);
    if (focusedCardIndex_ == last && row.moreBox != nullptr) {
        // «Ще» → push ScreenResults in browse mode, scoped to this row's
        // type. Provider = first provider that contributed to the row
        // (cheap routing — the catalog has at most one provider per
        // row in practice, since /api/home aggregates per type).
        const std::string &prov = !row.data.items.empty() && !row.data.items.front().providers.empty()
                                      ? row.data.items.front().providers.front()
                                      : std::string();
        // The browse section name is the row's type. The backend's
        // /api/browse?section=… expects the section id; we pass the
        // type literal as a best-effort (browse UI also accepts media-
        // type literals for the catalog-aggregated browsing case).
        auto *results = new ScreenResults(main_, prov, row.data.type,
                                          /*query*/ "", row.data.title);
        setChild(results);
        return;
    }
    // Resume row (issue #66) — route to ScreenContent with the
    // remembered groupKey. The screen's pre-focus logic (#67) lands
    // the user on the right chip + episode; we never auto-play.
    if (row.kind == "resume") {
        if (focusedCardIndex_ < 0 ||
            focusedCardIndex_ >= static_cast<int>(row.resumeEntries.size())) {
            return;
        }
        const auto &e = row.resumeEntries[focusedCardIndex_];
        auto *content = new ScreenContent(main_, e.groupKey,
                                          /*title*/ std::string());
        setChild(content);
        return;
    }
    if (focusedCardIndex_ < 0 ||
        focusedCardIndex_ >= static_cast<int>(row.data.items.size())) {
        return;
    }
    const auto &item = row.data.items[focusedCardIndex_];
    auto *content = new ScreenContent(main_, item.groupKey, item.title);
    setChild(content);
}

void ScreenHome::openSearch() {
    // The user can re-enter Home by pressing Circle on ScreenSearch.
    // We push ScreenSearch as a child and toggle visibility so the
    // menu doesn't sit on top.
    auto *search = new ScreenSearch(main_);
    setChild(search);
}

void ScreenHome::setChild(c2d::C2DObject *next) {
    if (child_ == next) return;
    if (child_ != nullptr) {
        main_->remove(child_);
        delete child_;
        child_ = nullptr;
    }
    child_ = next;
    if (child_ != nullptr) main_->add(child_);
}

void ScreenHome::showError(const std::string &msg) {
    loadState_ = LoadState::Failed;
    statusText_->setString(msg);
    errorBody_->setString(
        "Перевірте, чи увімкнено ПК,\nта Налаштування → Адреса сервера.");
    (void)msg;
    errorTitle_->setVisibility(c2d::Visibility::Visible);
    errorBody_->setVisibility(c2d::Visibility::Visible);
    errorRetryBox_->setVisibility(c2d::Visibility::Visible);
    errorRetryLabel_->setVisibility(c2d::Visibility::Visible);
    // Hide the row widgets and the loupe focus — nothing to navigate.
    cursor_->setVisibility(c2d::Visibility::Hidden);
    for (auto &r : rows_) {
        if (r.header) r.header->setVisibility(c2d::Visibility::Hidden);
        for (auto *c : r.cards) c->setVisibility(c2d::Visibility::Hidden);
        for (auto *t : r.titles) t->setVisibility(c2d::Visibility::Hidden);
        if (r.moreBox) r.moreBox->setVisibility(c2d::Visibility::Hidden);
        if (r.moreLabel) r.moreLabel->setVisibility(c2d::Visibility::Hidden);
    }
}

void ScreenHome::retry() {
    // Hide the error widgets, kick off a fresh fetch.
    errorTitle_->setVisibility(c2d::Visibility::Hidden);
    errorBody_->setVisibility(c2d::Visibility::Hidden);
    errorRetryBox_->setVisibility(c2d::Visibility::Hidden);
    errorRetryLabel_->setVisibility(c2d::Visibility::Hidden);
    statusText_->setString("Завантаження…");
    requestHome();
}

void ScreenHome::onUpdate() {
    // Pull the worker-thread response exactly once.
    if (homeFetched_.load(std::memory_order_acquire)) {
        homeFetched_.store(false, std::memory_order_release);
        if (loadState_ == LoadState::Loading) {
            if (fetchError_.empty()) {
                rebuildRows();
                loadState_ = LoadState::Loaded;
                statusText_->setString("Готово");
                layoutRows();
            } else {
                showError("Помилка: " + fetchError_);
            }
        }
    }

    const unsigned int keys = main_->getInput()->getKeys(0);

    // Error-screen mode: only the «Повторити» pill accepts input.
    if (loadState_ == LoadState::Failed) {
        if (keys & c2d::Input::Key::Fire1) {
            retry();
        } else if (keys & c2d::Input::Key::Fire2) {
            // Back to the menu.
            setVisibility(c2d::Visibility::Hidden, true);
        }
        RectangleShape::onUpdate();
        return;
    }

    if (focusRow_ == FocusRow::Loupe) {
        if (keys & c2d::Input::Key::Down) {
            moveFocusVertical(+1);
            layoutRows();
        } else if (keys & c2d::Input::Key::Fire1) {
            activateFocused();
        } else if (keys & c2d::Input::Key::Fire2) {
            setVisibility(c2d::Visibility::Hidden, true);
        }
        RectangleShape::onUpdate();
        return;
    }

    if (keys & c2d::Input::Key::Up) {
        moveFocusVertical(-1);
        layoutRows();
    } else if (keys & c2d::Input::Key::Down) {
        moveFocusVertical(+1);
        layoutRows();
    } else if (keys & c2d::Input::Key::Left) {
        if (moveFocusHorizontal(-1)) layoutRows();
    } else if (keys & c2d::Input::Key::Right) {
        if (moveFocusHorizontal(+1)) layoutRows();
    } else if (keys & c2d::Input::Key::Fire1) {
        activateFocused();
    } else if (keys & c2d::Input::Key::Fire2) {
        // Drop any pushed child before hiding — otherwise the menu
        // could re-show with a stale ScreenContent / ScreenResults
        // child attached.
        setChild(nullptr);
        setVisibility(c2d::Visibility::Hidden, true);
    }

    RectangleShape::onUpdate();
}

} // namespace cs
