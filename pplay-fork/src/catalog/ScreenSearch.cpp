#include "UiScale.h"
#include "ScreenSearch.h"
#include "CatalogContext.h"
#include "main.h"
#include "ScreenResults.h"

#include <cstdio>
#include <sstream>

namespace cs {

namespace {
// 10-foot scale: typography floor and action-safe margins anchored to
// 1080p (issue #57, v3 spec §5.1).
using ui::kSmallSize;
using ui::kBodySize;
using ui::kTitleSize;
using ui::kMarginX;
using ui::kMarginY;
using ui::kGap;
using ui::kFocusOutline;
using ui::kKeyCellW;
constexpr int kKeySize = kBodySize;
} // namespace

ScreenSearch::ScreenSearch(c2d::C2DRenderer *main)
    : RectangleShape({0, 0, static_cast<float>(static_cast<c2d::C2DRenderer *>(main)->getSize().x),
                      static_cast<float>(static_cast<c2d::C2DRenderer *>(main)->getSize().y)}),
      api_(CatalogContext::get()),
      main_(static_cast<Main *>(main)) {
    setFillColor(c2d::Color{0x12, 0x12, 0x12, 0xff});
    setLayer(5);

    const float W = static_cast<float>(static_cast<c2d::C2DRenderer *>(main)->getSize().x);
    const float H = static_cast<float>(static_cast<c2d::C2DRenderer *>(main)->getSize().y);

    title_ = new c2d::Text("Пошук", kTitleSize, main_->getFont());
    title_->setPosition({kMarginX, kMarginY});
    title_->setFillColor(c2d::Color{0xff, 0xff, 0xff, 0xff});
    add(title_);

    textLabel_ = new c2d::Text("", kBodySize, main_->getFont());
    textLabel_->setPosition({kMarginX, kMarginY + kTitleSize + 12});
    textLabel_->setFillColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
    add(textLabel_);

    keyboardText_ = new c2d::Text("", kKeySize, main_->getFont());
    keyboardText_->setPosition({kMarginX, kMarginY + kTitleSize + kBodySize + 28});
    keyboardText_->setFillColor(c2d::Color{0xee, 0xee, 0xee, 0xff});
    add(keyboardText_);

    cursor_ = new c2d::RectangleShape({0, 0, 0, 0});
    cursor_->setFillColor(c2d::Color{0x55, 0xef, 0xc4, 0x40});
    cursor_->setOutlineColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
    cursor_->setOutlineThickness(kFocusOutline);
    add(cursor_);

    statusText_ = new c2d::Text("", kSmallSize, main_->getFont());
    statusText_->setPosition({kMarginX, H - 2 * (kSmallSize + 8) - kMarginY});
    statusText_->setFillColor(c2d::Color{0xaa, 0xaa, 0xaa, 0xff});
    add(statusText_);

    helpText_ = new c2d::Text("X: вибрати · △: пробіл · □: видалити · Options: пошук · O: назад",
                              kSmallSize, main_->getFont());
    helpText_->setPosition({kMarginX, H - (kSmallSize + 8) - kMarginY});
    helpText_->setFillColor(c2d::Color{0x88, 0x88, 0x88, 0xff});
    add(helpText_);

    (void)W;
    renderKeyboard();
    setStatus("Готовий");
}

ScreenSearch::~ScreenSearch() {
    // Drop any pushed child (ScreenResults) so it doesn't stay around
    // as a hidden widget in `main_`'s children list.
    if (child_ != nullptr) {
        if (main_ != nullptr) main_->remove(child_);
        delete child_;
        child_ = nullptr;
    }
}

void ScreenSearch::setChild(c2d::C2DObject *next) {
    if (child_ == next) return;
    if (child_ != nullptr) {
        main_->remove(child_);
        delete child_;
        child_ = nullptr;
    }
    child_ = next;
    if (child_ != nullptr) {
        main_->add(child_);
    }
}

void ScreenSearch::renderKeyboard() {
    std::ostringstream oss;
    const int rows = kb_.rows();
    const int cols = kb_.cols();
    const float startX = kMarginX;
    const float startY = kMarginY + kTitleSize + kBodySize + 28;
    const float cellW = kKeyCellW;
    const float cellH = kKeySize + 12.0f;
    // Pre-position cursor behind the focused cell; reset size based on the
    // widest key in the grid (some Cyrillic glyphs are wider than 64px
    // so we pad the cell).
    const float cx = startX + kbCol_ * cellW;
    const float cy = startY + kbRow_ * cellH;
    cursor_->setPosition({cx, cy});
    cursor_->setSize({cellW, cellH});

    for (int r = 0; r < rows; ++r) {
        if (r > 0) oss << "\n";
        for (int c = 0; c < cols; ++c) {
            std::string action;
            const bool active = (r == kbRow_ && c == kbCol_);
            if (active) oss << "[";
            const std::string &label = kb_.labelAt(r, c);
            if (label.empty()) {
                // Action key (SPACE, BACK, OK, etc.) — show its action tag.
                if (kb_.isAction(r, c, action)) oss << action;
                else oss << " ";
            } else {
                oss << label;
            }
            if (active) oss << "]";
            // Single-space separator between cells (no wrapping math).
            if (c + 1 < cols) oss << " ";
        }
    }
    keyboardText_->setString(oss.str());
}

void ScreenSearch::renderStatus() {
    textLabel_->setString("Запит: " + kb_.text());
    if (!fetchError_.empty() && !inFlight_) {
        setStatus("Помилка: " + fetchError_);
    } else if (inFlight_) {
        setStatus("Шукаю…");
    } else if (resultCount_ == 0 && !kb_.text().empty()) {
        setStatus("Готово · 0 результатів");
    } else {
        setStatus(kb_.text().empty() ? "Введіть запит" : "Готово");
    }
}

void ScreenSearch::setStatus(const std::string &s) {
    if (statusText_) statusText_->setString(s);
}

void ScreenSearch::requestSearch() {
    if (!api_) {
        setStatus("Backend недоступний");
        return;
    }
    const std::string q = kb_.text();
    if (q.empty()) return;
    inFlight_ = true;
    searchFetched_.store(false, std::memory_order_release);
    api_->searchAsync(q, [this](bool ok, std::vector<SearchItem> results, std::string err) {
        fetchedResults_ = std::move(results);
        if (ok) {
            fetchError_.clear();
            resultCount_ = fetchedResults_.size();
        } else {
            fetchError_ = err.empty() ? "невідома помилка" : std::move(err);
            resultCount_ = 0;
        }
        inFlight_ = false;
        searchFetched_.store(true, std::memory_order_release);
    });
}

void ScreenSearch::onUpdate() {
    // Pull worker-thread result exactly once.
    if (searchFetched_.load(std::memory_order_acquire)) {
        searchFetched_.store(false, std::memory_order_release);
        renderStatus();
        if (!fetchError_.empty()) return;
        // On success with results, push ScreenResults in search mode.
        if (resultCount_ > 0) {
            auto *results = new ScreenResults(main_,
                                              /*provider*/ "",
                                              /*section*/ "",
                                              kb_.text(),
                                              "Результати пошуку");
            // Pass the fetched rows down by attaching them — the results
            // screen would otherwise refetch. The simplest correct path
            // is to let ScreenResults refetch with the same query, since
            // re-search is cheap and keeps the API uniform.
            setChild(results);
        }
        return;
    }

    const unsigned int keys = main_->getInput()->getKeys(0);

    if (keys & c2d::Input::Key::Up) {
        kbRow_ = (kbRow_ - 1 + kb_.rows()) % kb_.rows();
        renderKeyboard();
    } else if (keys & c2d::Input::Key::Down) {
        kbRow_ = (kbRow_ + 1) % kb_.rows();
        renderKeyboard();
    } else if (keys & c2d::Input::Key::Left) {
        kbCol_ = (kbCol_ - 1 + kb_.cols()) % kb_.cols();
        renderKeyboard();
    } else if (keys & c2d::Input::Key::Right) {
        kbCol_ = (kbCol_ + 1) % kb_.cols();
        renderKeyboard();
    } else if (keys & c2d::Input::Key::Fire3) {
        // Triangle — space
        kb_.appendUtf8(" ");
        renderKeyboard();
        renderStatus();
    } else if (keys & c2d::Input::Key::Fire4) {
        // Square — backspace
        kb_.backspace();
        renderKeyboard();
        renderStatus();
    } else if (keys & c2d::Input::Key::Fire1) {
        // Cross — pick focused cell
        std::string action;
        if (kb_.isAction(kbRow_, kbCol_, action)) {
            if (action == "SPACE") {
                kb_.appendUtf8(" ");
            } else if (action == "BACK") {
                kb_.backspace();
            } else if (action == "CLR") {
                kb_.clear();
            } else if (action == "OK") {
                if (!kb_.text().empty()) requestSearch();
            }
        } else {
            kb_.appendUtf8(kb_.labelAt(kbRow_, kbCol_));
        }
        renderKeyboard();
        renderStatus();
    } else if (keys & c2d::Input::Key::Start) {
        // Options — submit
        if (!kb_.text().empty()) requestSearch();
    } else if (keys & c2d::Input::Key::Fire2) {
        // Circle — back. Drop any pushed child first.
        setChild(nullptr);
        setVisibility(c2d::Visibility::Hidden, true);
    }

    RectangleShape::onUpdate();
}

} // namespace cs