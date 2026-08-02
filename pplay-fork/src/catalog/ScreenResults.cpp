#include "ScreenResults.h"
#include "CatalogContext.h"
#include "main.h"
#include "ScreenContent.h"

#include <cstdio>
#include <sstream>

namespace cs {

namespace {
constexpr int kTitleSize = 28;
constexpr int kBodySize = 18;
constexpr int kStatusSize = 16;
constexpr float kPanelPadding = 16.0f;
constexpr float kRowHeight = 56.0f;
constexpr int kPosterLazyLookahead = 2;
} // namespace

const char *ScreenResults::typeLabel(const std::string &type) {
    if (type == "movie") return "Фільм";
    if (type == "series") return "Серіал";
    if (type == "anime") return "Аніме";
    if (type == "cartoon") return "Мультфільм";
    if (type == "dorama") return "Дорама";
    return "?";
}

ScreenResults::ScreenResults(c2d::C2DRenderer *main,
                             std::string provider,
                             std::string section,
                             std::string query,
                             std::string title)
    : RectangleShape({0, 0, static_cast<float>(static_cast<c2d::C2DRenderer *>(main_)->getSize().x),
                      static_cast<float>(static_cast<c2d::C2DRenderer *>(main_)->getSize().y)}),
      api_(CatalogContext::get()),
      main_(static_cast<Main *>(main)),
      provider_(std::move(provider)),
      section_(std::move(section)),
      query_(std::move(query)) {
    setFillColor(c2d::Color{0x12, 0x12, 0x12, 0xff});
    setLayer(5);

    const float W = static_cast<float>(static_cast<c2d::C2DRenderer *>(main_)->getSize().x);
    const float H = static_cast<float>(static_cast<c2d::C2DRenderer *>(main_)->getSize().y);

    mode_ = query_.empty() ? Mode::Browse : Mode::Search;
    if (mode_ == Mode::Search) page_ = 1;

    title_ = new c2d::Text(title, kTitleSize, main_->getFont());
    title_->setPosition({kPanelPadding, kPanelPadding});
    title_->setFillColor(c2d::Color{0xff, 0xff, 0xff, 0xff});
    add(title_);

    // Poster thumbnail area on the left of the list, with a fallback
    // "no poster" fill when no texture is loaded.
    const float posterX = kPanelPadding;
    const float posterY = kPanelPadding + kTitleSize + 12;
    const float posterSize = kRowHeight * 2.0f;
    posterBox_ = new c2d::RectangleShape({posterX, posterY, posterSize, posterSize});
    posterBox_->setFillColor(c2d::Color{0x22, 0x22, 0x22, 0xff});
    posterBox_->setOutlineColor(c2d::Color{0x55, 0x55, 0x55, 0xff});
    posterBox_->setOutlineThickness(1.0f);
    add(posterBox_);

    posterBadge_ = new c2d::Text("—", kStatusSize, main_->getFont());
    posterBadge_->setPosition({posterX + 8, posterY + 8});
    posterBadge_->setFillColor(c2d::Color{0x66, 0x66, 0x66, 0xff});
    add(posterBadge_);

    // Rows to the right of the poster.
    const float rowsX = posterX + posterSize + 16;
    const float rowsW = W - rowsX - kPanelPadding;
    rowsText_ = new c2d::Text("", kBodySize, main_->getFont());
    rowsText_->setPosition({rowsX, posterY});
    rowsText_->setFillColor(c2d::Color{0xee, 0xee, 0xee, 0xff});
    add(rowsText_);

    cursor_ = new c2d::RectangleShape({rowsX, posterY, rowsW, kRowHeight});
    cursor_->setFillColor(c2d::Color{0x55, 0xef, 0xc4, 0x40});
    cursor_->setOutlineColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
    cursor_->setOutlineThickness(1.5f);
    add(cursor_);

    status_ = new c2d::Text("", kStatusSize, main_->getFont());
    status_->setPosition({kPanelPadding, H - kStatusSize - kPanelPadding});
    status_->setFillColor(c2d::Color{0xaa, 0xaa, 0xaa, 0xff});
    add(status_);

    requestPage(page_);
    setStatus("Завантаження…");
}

void ScreenResults::requestPage(int page) {
    if (!api_) {
        setStatus("Backend недоступний");
        return;
    }
    if (inFlight_) return;
    inFlight_ = true;
    pageFetched_.store(false, std::memory_order_release);
    if (mode_ == Mode::Browse) {
        api_->browseAsync(provider_, section_, page,
            [this](bool ok, BrowseItem b, std::string err) {
                if (ok) {
                    fetchedItems_ = std::move(b.results);
                    fetchedHasNext_ = b.hasNext;
                    fetchError_.clear();
                } else {
                    fetchedItems_.clear();
                    fetchedHasNext_ = false;
                    fetchError_ = err.empty() ? "невідома помилка" : std::move(err);
                }
                inFlight_ = false;
                pageFetched_.store(true, std::memory_order_release);
            });
    } else {
        api_->searchAsync(query_,
            [this](bool ok, std::vector<SearchItem> r, std::string err) {
                if (ok) {
                    fetchedItems_ = std::move(r);
                    fetchedHasNext_ = false;
                    fetchError_.clear();
                } else {
                    fetchedItems_.clear();
                    fetchedHasNext_ = false;
                    fetchError_ = err.empty() ? "невідома помилка" : err;
                }
                inFlight_ = false;
                pageFetched_.store(true, std::memory_order_release);
            });
    }
}

void ScreenResults::requestNextIfNeeded() {
    if (mode_ != Mode::Browse || !hasNext_ || inFlight_) return;
    if (selection_ < static_cast<int>(items_.size()) - 1) return;
    requestPage(page_ + 1);
}

void ScreenResults::requestPosterForRow(size_t rowIndex) {
    if (!api_) return;
    if (rowIndex >= items_.size()) return;
    const auto &url = items_[rowIndex].poster;
    if (url.empty()) return;
    api_->loadPoster(url,
        [this, rowIndex](bool /*ok*/, std::vector<std::uint8_t> /*bytes*/,
                         std::string /*ct*/, std::string /*err*/) {
            // Touching c2d textures off the UI thread is unsafe; the
            // texture wiring lives in the UI thread tick below.
            (void)rowIndex;
            posterRefresh_.store(true, std::memory_order_release);
        });
}

void ScreenResults::renderRows() {
    std::ostringstream oss;
    for (size_t i = 0; i < items_.size(); ++i) {
        const auto &it = items_[i];
        if (i > 0) oss << "\n";
        const char *badge = typeLabel(it.type);
        oss << "[" << badge << "] " << it.title;
        if (it.year > 0) oss << " (" << it.year << ")";
    }
    rowsText_->setString(oss.str());

    const float rowsX = rowsText_->getPosition().x;
    const float posterY = posterBox_->getPosition().y;
    cursor_->setPosition({rowsX - 4, posterY + selection_ * kRowHeight});
    cursor_->setSize({rowsText_->getSize().x + 8, kRowHeight});

    // Right-side poster placeholder metadata.
    if (selection_ >= 0 && selection_ < static_cast<int>(items_.size())) {
        const auto &it = items_[selection_];
        std::ostringstream badge;
        badge << typeLabel(it.type);
        if (it.year > 0) badge << " · " << it.year;
        if (!it.provider.empty()) badge << " · " << it.provider;
        posterBadge_->setString(badge.str());
    }
}

void ScreenResults::setStatus(const std::string &s) {
    if (status_) status_->setString(s);
}

void ScreenResults::onUpdate() {
    // Pull worker-thread result exactly once.
    if (pageFetched_.load(std::memory_order_acquire)) {
        pageFetched_.store(false, std::memory_order_release);
        items_ = std::move(fetchedItems_);
        hasNext_ = fetchedHasNext_;
        selection_ = 0;
        renderRows();
        if (!fetchError_.empty()) {
            setStatus("Помилка: " + fetchError_);
        } else if (items_.empty()) {
            setStatus("Нічого не знайдено");
        } else {
            std::string s = "Готово · " + std::to_string(items_.size()) + " результатів";
            if (hasNext_) s += " · є ще";
            setStatus(s);
            requestPosterForRow(0);
            requestPosterForRow(1);
        }
    }

    const unsigned int keys = main_->getInput()->getKeys(0);
    const int total = static_cast<int>(items_.size());

    if (keys & c2d::Input::Key::Up) {
        if (total > 0) {
            selection_ = (selection_ - 1 + total) % total;
            renderRows();
            if (selection_ >= 1) requestPosterForRow(selection_ - 1);
            requestPosterForRow(selection_);
            requestPosterForRow(selection_ + kPosterLazyLookahead);
            requestNextIfNeeded();
        }
    } else if (keys & c2d::Input::Key::Down) {
        if (total > 0) {
            selection_ = (selection_ + 1) % total;
            renderRows();
            if (selection_ >= 1) requestPosterForRow(selection_ - 1);
            requestPosterForRow(selection_);
            requestPosterForRow(selection_ + kPosterLazyLookahead);
            requestNextIfNeeded();
        }
    } else if (keys & c2d::Input::Key::Right) {
        if (mode_ == Mode::Browse && hasNext_ && !inFlight_) requestPage(page_ + 1);
    } else if (keys & c2d::Input::Key::Left) {
        if (page_ > 1 && !inFlight_ && mode_ == Mode::Browse) requestPage(page_ - 1);
    } else if (keys & c2d::Input::Key::Fire1) {
        // Open the focused item's content screen.
        if (selection_ >= 0 && selection_ < total) {
            const auto &it = items_[selection_];
            auto *content = new ScreenContent(main_, it.id, it.title);
            main_->add(content);
        }
    } else if (keys & c2d::Input::Key::Fire2) {
        // Back — hide; in browse mode this returns to ScreenSections,
        // in search mode this returns to ScreenSearch. Both stay in the
        // scene tree so popping is enough.
        setVisibility(c2d::Visibility::Hidden, true);
    }

    RectangleShape::onUpdate();
}

} // namespace cs