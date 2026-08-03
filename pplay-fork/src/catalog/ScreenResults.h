#pragma once

#include "main.h"
#include "CatalogApi.h"
#include "cross2d/c2d.h"

#include <atomic>
#include <cstdint>
#include <string>
#include <vector>

namespace cs {

// Browses either:
//   - a provider + section (browse mode, paginates via has_next), or
//   - a search query (search mode, no pagination).
//
// Layout: vertical list of rows showing type badge · title · year, with
// the poster loaded lazily per row (loadPoster on the focused row plus
// the next two so navigation feels instant).
class ScreenResults : public c2d::RectangleShape {
public:
    ScreenResults(c2d::C2DRenderer *main,
                  std::string provider,
                  std::string section,
                  std::string query,
                  std::string title);
    ~ScreenResults() override;

    void onUpdate() override;

private:
    enum class Mode { Browse, Search };

    void requestPage(int page);
    void requestNextIfNeeded();
    void requestPosterForRow(size_t rowIndex);
    void renderRows();
    void setStatus(const std::string &s);
    void applyPosterTexture(); // UI-thread: decode posterBytes_ into posterTex_
    // Remove + delete the previously pushed child screen, if any,
    // and (optionally) install `next` as the new child. Avoids the
    // widget leak when the user re-pushes the same screen type.
    void setChild(c2d::C2DObject *next);

    static const char *typeLabel(const std::string &type);

    CatalogApi *api_ = nullptr;
    Main *main_ = nullptr;
    c2d::C2DObject *child_ = nullptr; // owned — freed by setChild / dtor

    Mode mode_ = Mode::Browse;
    std::string provider_;
    std::string section_;
    std::string query_;
    int page_ = 1;
    bool hasNext_ = false;

    c2d::Text *title_ = nullptr;
    c2d::Text *status_ = nullptr;
    c2d::Text *rowsText_ = nullptr;
    c2d::RectangleShape *cursor_ = nullptr;
    c2d::RectangleShape *posterBox_ = nullptr;
    c2d::Texture *posterTex_ = nullptr;
    c2d::Text *posterBadge_ = nullptr;

    std::vector<SearchItem> items_;
    int selection_ = 0;
    bool inFlight_ = false;
    std::atomic<bool> pageFetched_{false};
    std::atomic<bool> posterRefresh_{false};
    std::vector<SearchItem> fetchedItems_;
    bool fetchedHasNext_ = false;
    std::string fetchError_;

    // Pending poster fetched off-thread; consumed on the UI thread.
    std::vector<std::uint8_t> posterBytes_;
    size_t pendingPosterRow_ = static_cast<size_t>(-1);
    size_t displayedPosterRow_ = static_cast<size_t>(-1);
};

} // namespace cs