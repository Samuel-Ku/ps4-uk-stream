#include "ScreenSections.h"
#include "CatalogContext.h"
#include "main.h"
#include "ScreenResults.h"

#include <cstdio>
#include <sstream>

namespace cs {

namespace {

// Two-column layout constants. The screen is 1280x720 (PS4) but Main
// scales via Vector2f scaling — we render against the renderer size so
// the layout adapts to 1920x1080 too.
constexpr int kTitleSize = 28;
constexpr int kBodySize = 20;
constexpr int kStatusSize = 18;
constexpr float kPanelPadding = 16.0f;

} // namespace

ScreenSections::ScreenSections(c2d::C2DRenderer *main)
    : RectangleShape({0, 0, static_cast<float>(static_cast<c2d::C2DRenderer *>(main)->getSize().x),
                      static_cast<float>(static_cast<c2d::C2DRenderer *>(main)->getSize().y)}),
      api_(CatalogContext::get()),
      main_(static_cast<Main *>(main)) {
    setFillColor(c2d::Color{0x12, 0x12, 0x12, 0xff});
    setLayer(5);

    const float W = static_cast<float>(static_cast<c2d::C2DRenderer *>(main)->getSize().x);
    const float H = static_cast<float>(static_cast<c2d::C2DRenderer *>(main)->getSize().y);

    title_ = new c2d::Text("Каталог UA", kTitleSize, main_->getFont());
    title_->setPosition({kPanelPadding, kPanelPadding});
    title_->setFillColor(c2d::Color{0xff, 0xff, 0xff, 0xff});
    add(title_);

    status_ = new c2d::Text("Завантаження…", kStatusSize, main_->getFont());
    status_->setPosition({kPanelPadding, kPanelPadding + kTitleSize + 8});
    status_->setFillColor(c2d::Color{0xaa, 0xaa, 0xaa, 0xff});
    add(status_);

    // Left column: providers.
    const float colY = kPanelPadding + kTitleSize + kStatusSize + 24;
    const float colH = H - colY - kPanelPadding;
    const float leftW = W * 0.38f;
    providerPanel_ = new c2d::RectangleShape({kPanelPadding, colY, leftW - kPanelPadding, colH});
    providerPanel_->setFillColor(c2d::Color{0x1e, 0x1e, 0x1e, 0xff});
    providerPanel_->setOutlineColor(c2d::Color{0x55, 0x55, 0x55, 0xff});
    providerPanel_->setOutlineThickness(1.0f);
    add(providerPanel_);

    // Right column: sections.
    const float rightX = leftW + kPanelPadding;
    const float rightW = W - rightX - kPanelPadding;
    sectionPanel_ = new c2d::RectangleShape({rightX, colY, rightW, colH});
    sectionPanel_->setFillColor(c2d::Color{0x1a, 0x1a, 0x1a, 0xff});
    sectionPanel_->setOutlineColor(c2d::Color{0x55, 0x55, 0x55, 0xff});
    sectionPanel_->setOutlineThickness(1.0f);
    add(sectionPanel_);

    providerList_ = new c2d::Text("", kBodySize, main_->getFont());
    providerList_->setPosition({kPanelPadding + 8, colY + 8});
    providerList_->setFillColor(c2d::Color{0xff, 0xff, 0xff, 0xff});
    add(providerList_);

    sectionList_ = new c2d::Text("", kBodySize, main_->getFont());
    sectionList_->setPosition({rightX + 8, colY + 8});
    sectionList_->setFillColor(c2d::Color{0xdd, 0xdd, 0xdd, 0xff});
    add(sectionList_);

    // Cursor highlights the active row in the active column.
    cursor_ = new c2d::RectangleShape({0, 0, leftW - kPanelPadding - 16, kBodySize + 8});
    cursor_->setFillColor(c2d::Color{0x55, 0xef, 0xc4, 0x40});
    cursor_->setOutlineColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
    cursor_->setOutlineThickness(1.5f);
    cursor_->setVisibility(c2d::Visibility::Hidden, false);
    add(cursor_);

    requestSections();
}

ScreenSections::~ScreenSections() {
    // Drop any child we pushed. Removing from main_ is a no-op if
    // we're being destroyed because main_ itself is going away (the
    // children list is being torn down too), but it's a real op
    // when the user navigated back and we removed ourselves first.
    if (child_ != nullptr) {
        if (main_ != nullptr) main_->remove(child_);
        delete child_;
        child_ = nullptr;
    }
}

void ScreenSections::requestSections() {
    if (!api_) {
        loadState_ = LoadState::Failed;
        setStatus("Backend недоступний");
        return;
    }
    loadState_ = LoadState::Loading;
    setStatus("Завантаження розділів…");
    sectionsFetched_ = false;
    // Note: callback fires on the api worker thread; only the atomic
    // flag and the fetched vectors are touched off-thread, never c2d.
    api_->sectionsAsync([this](bool ok, std::vector<ProviderSections> providers, std::string err) {
        if (ok) {
            fetchedProviders_ = std::move(providers);
            fetchError_.clear();
        } else {
            fetchedProviders_.clear();
            fetchError_ = err.empty() ? "невідома помилка" : std::move(err);
        }
        sectionsFetched_.store(true, std::memory_order_release);
    });
}

void ScreenSections::setStatus(const std::string &s) {
    if (status_) status_->setString(s);
}

void ScreenSections::renderLabels() {
    // Provider list — one per line.
    std::ostringstream pss;
    for (size_t i = 0; i < providers_.size(); ++i) {
        const auto &ps = providers_[i];
        if (i == static_cast<size_t>(providerIndex_)) pss << "> ";
        else pss << "  ";
        pss << (ps.name.empty() ? ps.provider : ps.name);
        pss << "\n";
    }
    providerList_->setString(pss.str());

    // Section list for the currently focused provider.
    std::ostringstream sss;
    if (providerIndex_ >= 0 && providerIndex_ < static_cast<int>(providers_.size())) {
        const auto &secs = providers_[providerIndex_].sections;
        for (size_t i = 0; i < secs.size(); ++i) {
            if (i == static_cast<size_t>(sectionIndex_)) sss << "> ";
            else sss << "  ";
            sss << secs[i].title;
            sss << "\n";
        }
    }
    sectionList_->setString(sss.str());

    // Cursor placement.
    const float colY = providerPanel_->getPosition().y;
    const float rowH = kBodySize + 6.0f;
    if (column_ == Column::Providers) {
        const float x = kPanelPadding + 8;
        const float y = colY + 8 + providerIndex_ * rowH - 4;
        cursor_->setPosition({x, y});
        cursor_->setSize({providerPanel_->getSize().x - 16, kBodySize + 8});
    } else {
        const float x = sectionPanel_->getPosition().x + 8;
        const float y = colY + 8 + sectionIndex_ * rowH - 4;
        cursor_->setPosition({x, y});
        cursor_->setSize({sectionPanel_->getSize().x - 16, kBodySize + 8});
    }
    cursor_->setVisibility(c2d::Visibility::Visible, false);
}

void ScreenSections::onProviderChanged() {
    sectionIndex_ = 0;
    renderLabels();
}

void ScreenSections::onSectionActivated() {
    if (providerIndex_ < 0 || providerIndex_ >= static_cast<int>(providers_.size())) return;
    const auto &ps = providers_[providerIndex_];
    if (sectionIndex_ < 0 || sectionIndex_ >= static_cast<int>(ps.sections.size())) return;
    auto *results = new ScreenResults(main_, ps.provider, ps.sections[sectionIndex_].id,
                                      /*query*/ "", ps.sections[sectionIndex_].title);
    setChild(results);
}

void ScreenSections::setChild(c2d::C2DObject *next) {
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

void ScreenSections::onUpdate() {
    // Pull the worker-thread result exactly once.
    if (sectionsFetched_.load(std::memory_order_acquire)) {
        sectionsFetched_.store(false, std::memory_order_release);
        if (fetchedProviders_.empty()) {
            loadState_ = LoadState::Failed;
            setStatus("Помилка: " + fetchError_);
        } else {
            providers_ = std::move(fetchedProviders_);
            loadState_ = LoadState::Loaded;
            setStatus("Готово · " + std::to_string(providers_.size()) + " провайдерів");
            providerIndex_ = 0;
            sectionIndex_ = 0;
        }
        renderLabels();
    }

    if (loadState_ != LoadState::Loaded) {
        RectangleShape::onUpdate();
        return;
    }

    const unsigned int keys = main_->getInput()->getKeys(0);
    auto moveCursor = [](int current, int delta, int max) -> int {
        if (max <= 0) return 0;
        int v = (current + delta + max) % max;
        return v;
    };

    if (keys & c2d::Input::Key::Up) {
        if (column_ == Column::Providers) {
            const int n = static_cast<int>(providers_.size());
            providerIndex_ = moveCursor(providerIndex_, -1, n);
        } else {
            const int n = providers_.empty() ? 0
                : static_cast<int>(providers_[providerIndex_].sections.size());
            sectionIndex_ = moveCursor(sectionIndex_, -1, n);
        }
        renderLabels();
    } else if (keys & c2d::Input::Key::Down) {
        if (column_ == Column::Providers) {
            const int n = static_cast<int>(providers_.size());
            providerIndex_ = moveCursor(providerIndex_, 1, n);
        } else {
            const int n = providers_.empty() ? 0
                : static_cast<int>(providers_[providerIndex_].sections.size());
            sectionIndex_ = moveCursor(sectionIndex_, 1, n);
        }
        renderLabels();
    } else if (keys & c2d::Input::Key::Right) {
        if (column_ == Column::Providers) {
            column_ = Column::Sections;
            renderLabels();
        }
    } else if (keys & c2d::Input::Key::Left) {
        if (column_ == Column::Sections) {
            column_ = Column::Providers;
            renderLabels();
        }
    } else if (keys & c2d::Input::Key::Fire1) {
        if (column_ == Column::Sections) {
            onSectionActivated();
        } else {
            column_ = Column::Sections;
            renderLabels();
        }
    } else if (keys & c2d::Input::Key::Fire2) {
        // Back — hide the screen, return to the main menu. Drop any
        // pushed child first so it doesn't stay around as a hidden
        // widget in `main_`'s children list.
        setChild(nullptr);
        setVisibility(c2d::Visibility::Hidden, true);
    }

    RectangleShape::onUpdate();
}

} // namespace cs