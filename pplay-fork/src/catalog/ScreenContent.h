#pragma once

#include "main.h"
#include "CatalogApi.h"
#include "ChipStrip.h"
#include "cross2d/c2d.h"

#include <atomic>
#include <string>
#include <vector>

namespace cs {

// Detail screen for a single catalog item.
//
// Layout (top-down):
//   1. Title + type/year/provider metadata line
//   2. Source-chip strip — one chip per provider that served this
//      group (issue #62 / v3 spec §3.3). Left/Right moves focus,
//      Cross refetches content under that provider. Disabled chips
//      (provider status == Down) are grayed but still visible.
//   3. Description (word-wrapped, truncated to fit)
//   4. Content-level translations (only when translationsLevel == "content")
//   5. Season strip (left-to-right tabs)
//   6. Episode list (vertical)
//   7. Status line
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
    // Build the chip strip from item_.sources after a content fetch
    // lands. Called from renderAll() so the strip updates alongside
    // any chip-switch refetch.
    void rebuildChipStrip();
    // Default focus on the action row (the «Дивитись» implied button).
    // The screen has no explicit button widget yet — the entire
    // episode / movie area is the action target — so we keep the
    // focus cursor on the action row and disable chip-strip input
    // when the action row is "active" (i.e. when the user hasn't
    // explicitly entered the chip strip).
    void focusActionRow();
    void focusChipStrip();
    // Issue #67: pre-focus the chip strip on the remembered source for
    // a series group, and pre-select the remembered translation on the
    // action row. Falls through to a healthy source when the
    // remembered provider is DOWN (memory stays — the user might come
    // back when the source is back up). Movies never have a memory
    // entry (the policy lives at the call site: shouldRememberMemory
    // in CatalogApi.h), so this is a no-op for them.
    void applyMemoryPreFocus();
    // Whether the chip strip owns the current focus / input.
    bool chipStripHasFocus() const { return focusMode_ == FocusMode::Chips; }

    enum class FocusMode { Action, Chips };
    FocusMode focusMode_ = FocusMode::Action;

    CatalogApi *api_ = nullptr;
    Main *main_ = nullptr;
    std::string id_;
    // Cross-provider group identity for the active content (issue #69).
    // Captured from item_.groupKey on the first fetch; reused for
    // chip-switch refetches via /api/content/{groupKey}?source=<p>.
    std::string groupKey_;
    std::string pendingTitle_;

    c2d::Text *title_ = nullptr;
    c2d::Text *meta_ = nullptr;
    c2d::Text *description_ = nullptr;
    c2d::Text *translationsLabel_ = nullptr;
    c2d::Text *seasonsLabel_ = nullptr;
    c2d::Text *episodesLabel_ = nullptr;
    c2d::Text *status_ = nullptr;
    c2d::RectangleShape *cursor_ = nullptr;
    ui::ChipStrip *chipStrip_ = nullptr;

    ContentItem item_;
    std::atomic<bool> contentFetched_{false};
    ContentItem fetchedItem_;
    std::string fetchError_;

    int seasonIndex_ = 0;
    int episodeIndex_ = 0;
    int episodeTranslationIndex_ = 0;
    // Issue #67 — pre-selected content-level translation (when
    // translationsLevel == "content"). Driven by memory when present;
    // falls back to 0 (the first translation, the backend's default).
    int contentTranslationIndex_ = 0;

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