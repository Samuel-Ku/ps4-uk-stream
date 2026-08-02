#pragma once

#include "main.h"
#include "CatalogApi.h"
#include "cross2d/c2d.h"

#include <atomic>
#include <string>
#include <vector>

namespace cs {

// Two-column browser: providers (left) → sections (right).
//
// On enter: triggers sectionsAsync(); the worker-thread callback sets
// `sectionsFetched_` and copies the result into `providers_`. The UI
// thread polls `sectionsFetched_` in onUpdate() and refreshes labels
// exactly once. No c2d widget is touched off the UI thread.
//
// Selecting a section (Fire1 on the right column) fires browseAsync()
// and pushes ScreenResults in browse mode.
class ScreenSections : public c2d::RectangleShape {
public:
    explicit ScreenSections(c2d::C2DRenderer *main);
    ~ScreenSections() override;

    void onUpdate() override;

private:
    enum class Column { Providers, Sections };
    enum class LoadState { Idle, Loading, Loaded, Failed };

    void requestSections();
    void renderLabels();
    void onProviderChanged();
    void onSectionActivated();
    void setStatus(const std::string &s);
    // Remove + delete the previously pushed child screen, if any,
    // and (optionally) install `next` as the new child. Avoids the
    // widget leak when the user re-pushes the same screen type.
    void setChild(c2d::C2DObject *next);

    CatalogApi *api_ = nullptr;
    Main *main_ = nullptr;
    c2d::C2DObject *child_ = nullptr; // owned — freed by setChild / dtor

    c2d::Text *title_ = nullptr;
    c2d::Text *status_ = nullptr;
    c2d::Text *providerList_ = nullptr;
    c2d::Text *sectionList_ = nullptr;
    c2d::RectangleShape *providerPanel_ = nullptr;
    c2d::RectangleShape *sectionPanel_ = nullptr;
    c2d::RectangleShape *cursor_ = nullptr;

    std::vector<ProviderSections> providers_;
    LoadState loadState_ = LoadState::Idle;
    std::atomic<bool> sectionsFetched_{false};
    std::vector<ProviderSections> fetchedProviders_;
    std::string fetchError_;
    int providerIndex_ = 0;
    int sectionIndex_ = 0;
    Column column_ = Column::Providers;
};

} // namespace cs