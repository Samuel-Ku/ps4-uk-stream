#pragma once

#include "main.h"
#include "CatalogApi.h"
#include "cross2d/c2d.h"

#include <atomic>
#include <string>
#include <vector>

namespace cs {

// Detail screen for a single catalog item.
//
// Layout (top-down):
//   1. Title + type/year/provider metadata line
//   2. Description (word-wrapped, truncated to fit)
//   3. Content-level translations (only when translationsLevel == "content")
//   4. Season strip (left-to-right tabs)
//   5. Episode list (vertical)
//   6. Status line
//
// translations_level="episode" hides the content-level translations row
// and instead shows per-episode translation choices in the episode row.
//
// Player hand-off: when Fire1 is pressed on an episode (or directly on
// the screen in the "no seasons" movie case), ScreenContent calls
// streamAsync(id, translation) and on success applies the resolved URL
// + headers to the existing Player via Main::getPlayer(). Headers go
// through mpv_command_string("set http-header-fields …") before load,
// so the existing Player::load(MediaFile) entry point is preserved.
class ScreenContent : public c2d::RectangleShape {
public:
    ScreenContent(c2d::C2DRenderer *main, std::string id, std::string title);
    ~ScreenContent() override = default;

    void onUpdate() override;

private:
    void requestContent();
    void renderAll();
    void setStatus(const std::string &s);
    void playEpisode(int seasonIdx, int epIdx, const std::string &translationId);
    static std::string wrapDescription(const std::string &desc, size_t maxLine);

    CatalogApi *api_ = nullptr;
    Main *main_ = nullptr;
    std::string id_;
    std::string pendingTitle_;

    c2d::Text *title_ = nullptr;
    c2d::Text *meta_ = nullptr;
    c2d::Text *description_ = nullptr;
    c2d::Text *translationsLabel_ = nullptr;
    c2d::Text *seasonsLabel_ = nullptr;
    c2d::Text *episodesLabel_ = nullptr;
    c2d::Text *status_ = nullptr;
    c2d::RectangleShape *cursor_ = nullptr;

    ContentItem item_;
    std::atomic<bool> contentFetched_{false};
    ContentItem fetchedItem_;
    std::string fetchError_;

    int seasonIndex_ = 0;
    int episodeIndex_ = 0;
    int episodeTranslationIndex_ = 0;

    // Pending stream hand-off.
    std::atomic<bool> streamFetched_{false};
    std::string streamUrl_;
    std::string streamTitle_;
    std::vector<std::pair<std::string, std::string>> streamHeaders_;
    std::string streamError_;

    // What is actually being played (for the #55 resume/memory store):
    // captured in playEpisode(), consumed at the player hand-off.
    std::string pendingPlayEpisodeId_;
    std::string pendingPlayTranslationLabel_;
};

} // namespace cs