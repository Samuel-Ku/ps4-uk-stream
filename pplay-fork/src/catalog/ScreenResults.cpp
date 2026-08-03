#include "UiScale.h"
#include "ScreenResults.h"
#include "CatalogContext.h"
#include "main.h"
#include "ScreenContent.h"

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
using ui::drawFocusBox;
using ui::kRowHeight;
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
    : RectangleShape({0, 0, static_cast<float>(static_cast<c2d::C2DRenderer *>(main)->getSize().x),
                      static_cast<float>(static_cast<c2d::C2DRenderer *>(main)->getSize().y)}),
      api_(CatalogContext::get()),
      main_(static_cast<Main *>(main)),
      provider_(std::move(provider)),
      section_(std::move(section)),
      query_(std::move(query)) {
    setFillColor(c2d::Color{0x12, 0x12, 0x12, 0xff});
    setLayer(5);

    const float W = static_cast<float>(static_cast<c2d::C2DRenderer *>(main)->getSize().x);
    const float H = static_cast<float>(static_cast<c2d::C2DRenderer *>(main_)->getSize().y);

    mode_ = query_.empty() ? Mode::Browse : Mode::Search;
    if (mode_ == Mode::Search) page_ = 1;

    title_ = new c2d::Text(title, kTitleSize, main_->getFont());
    title_->setPosition({kMarginX, kMarginY});
    title_->setFillColor(c2d::Color{0xff, 0xff, 0xff, 0xff});
    add(title_);

    // Poster thumbnail area on the left of the list, with a fallback
    // "no poster" fill when no texture is loaded.
    const float posterX = kMarginX;
    const float posterY = kMarginY + kTitleSize + 12;
    const float posterSize = kRowHeight * 2.0f;
    posterBox_ = new c2d::RectangleShape({posterX, posterY, posterSize, posterSize});
    posterBox_->setFillColor(c2d::Color{0x22, 0x22, 0x22, 0xff});
    posterBox_->setOutlineColor(c2d::Color{0x55, 0x55, 0x55, 0xff});
    posterBox_->setOutlineThickness(1.0f);
    add(posterBox_);

    posterBadge_ = new c2d::Text("—", kSmallSize, main_->getFont());
    posterBadge_->setPosition({posterX + 8, posterY + 8});
    posterBadge_->setFillColor(c2d::Color{0x66, 0x66, 0x66, 0xff});
    add(posterBadge_);

    // Rows to the right of the poster.
    const float rowsX = posterX + posterSize + kGap;
    const float rowsW = W - rowsX - kMarginX;
    rowsText_ = new c2d::Text("", kBodySize, main_->getFont());
    rowsText_->setPosition({rowsX, posterY});
    rowsText_->setFillColor(c2d::Color{0xee, 0xee, 0xee, 0xff});
    add(rowsText_);

    cursor_ = new c2d::RectangleShape({rowsX, posterY, rowsW, kRowHeight});
    cursor_->setFillColor(c2d::Color{0x55, 0xef, 0xc4, 0x40});
    cursor_->setOutlineColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
    cursor_->setOutlineThickness(kFocusOutline);
    add(cursor_);

    status_ = new c2d::Text("", kSmallSize, main_->getFont());
    status_->setPosition({kMarginX, H - kSmallSize - kMarginY});
    status_->setFillColor(c2d::Color{0xaa, 0xaa, 0xaa, 0xff});
    add(status_);

    requestPage(page_);
    setStatus("Завантаження…");
}

ScreenResults::~ScreenResults() {
    // Drop any pushed child screen (ScreenContent) so it doesn't stay
    // around as a hidden widget in `main_`'s children list.
    if (child_ != nullptr) {
        if (main_ != nullptr) main_->remove(child_);
        delete child_;
        child_ = nullptr;
    }
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
        [this, rowIndex](bool ok, std::vector<std::uint8_t> bytes,
                         std::string /*ct*/, std::string /*err*/) {
            // Touching c2d textures off the UI thread is unsafe; the
            // texture wiring lives in the UI thread tick below. We
            // stage the bytes + which row they belong to in members;
            // applyPosterTexture() in onUpdate does the actual decode
            // and Texture allocation.
            if (!ok) {
                posterRefresh_.store(true, std::memory_order_release);
                return;
            }
            posterBytes_ = std::move(bytes);
            pendingPosterRow_ = rowIndex;
            posterRefresh_.store(true, std::memory_order_release);
        });
}

void ScreenResults::applyPosterTexture() {
    if (!posterRefresh_.load(std::memory_order_acquire)) return;
    posterRefresh_.store(false, std::memory_order_release);

    // Nothing to decode (e.g. failure callback without bytes) — leave
    // the existing poster texture in place.
    if (posterBytes_.empty()) return;
    if (pendingPosterRow_ == displayedPosterRow_) {
        // Same row as currently displayed; the texture is already
        // up to date. Drop the bytes and bail.
        posterBytes_.clear();
        return;
    }

    // Tear down the previous texture (if any). The c2d::Texture
    // pointer was added to `this` (a RectangleShape) by the previous
    // applyPosterTexture call, so we have to remove it before
    // deleting or the children list keeps a dangling pointer.
    if (posterTex_) {
        remove(posterTex_);
        delete posterTex_;
        posterTex_ = nullptr;
    }

    // c2d::Texture(buffer, size) decodes the bytes via stb_image in
    // libcross2d. On PS4 the constructor is a no-op stub
    // (libcross2d/source/platforms/ps4/gl_renderer_stub.cpp) so the
    // texture is created with `available = false` and we skip
    // adding it to the scene — the posterBox_ placeholder stays
    // visible. This matches the existing PS4 build behavior.
    auto *tex = new c2d::Texture(posterBytes_.data(),
                                 static_cast<int>(posterBytes_.size()));
    if (tex->available) {
        const auto boxSize = posterBox_->getSize();
        tex->setSize(boxSize);
        // Match the placeholder's top-left so the texture lines up
        // exactly over the box.
        tex->setPosition(posterBox_->getPosition());
        add(tex);
        posterTex_ = tex;
        displayedPosterRow_ = pendingPosterRow_;
    } else {
        delete tex;
    }
    posterBytes_.clear();
}

void ScreenResults::setChild(c2d::C2DObject *next) {
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
    // Cursor scaled 1.05 about its center on top of the outline
    // (v3 spec §5.1, issue #75; math lives in UiScale.h).
    drawFocusBox(cursor_, {rowsX - 4, posterY + selection_ * kRowHeight,
                           rowsText_->getSize().x + 8, kRowHeight},
                 kFocusOutline);

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

    // Decode any poster bytes that arrived off-thread. Called every
    // tick so we drain the queue even when the page doesn't refresh
    // (lazy lookahead via requestPosterForRow sets the flag without
    // changing pageFetched_).
    applyPosterTexture();

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
    } else if (keys & c2d::Input::Key::Fire6) {
        // R1 — next browse page (v3 control scheme, issue #56)
        if (mode_ == Mode::Browse && hasNext_ && !inFlight_) requestPage(page_ + 1);
    } else if (keys & c2d::Input::Key::Fire5) {
        // L1 — previous browse page
        if (page_ > 1 && !inFlight_ && mode_ == Mode::Browse) requestPage(page_ - 1);
    } else if (keys & c2d::Input::Key::Fire1) {
        // Cross — open the focused item's content screen.
        if (selection_ >= 0 && selection_ < total) {
            const auto &it = items_[selection_];
            setChild(new ScreenContent(main_, it.id, it.title));
        }
    } else if (keys & c2d::Input::Key::Fire2) {
        // Circle — back; in browse mode this returns to ScreenSections,
        // in search mode this returns to ScreenSearch. Drop any
        // pushed child first so it doesn't stay around as a hidden
        // widget in `main_`'s children list.
        setChild(nullptr);
        setVisibility(c2d::Visibility::Hidden, true);
    }

    RectangleShape::onUpdate();
}

} // namespace cs