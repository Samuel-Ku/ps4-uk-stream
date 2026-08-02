#pragma once

// Horizontal chip strip widget (issue #62, v3 spec §3.3).
//
// A ChipStrip is a fixed-position row of selectable pills. Each chip
// carries a provider id and a label; the strip is stateless about which
// chip is "active" in the data sense — it tracks only the FOCUS index and
// fires a callback when the user confirms the focused chip. The parent
// screen is responsible for actually performing the refetch.
//
// Grayed-down chips (provider status == DOWN) still render — they're
// visible to the user but skipped on input and drawn in a muted shade
// so the user can see WHERE the source is, even if it isn't selectable
// right now. This matches the v3 spec's "respect provider status without
// hiding the source" rule.
//
// The strip owns a focus cursor (c2d::RectangleShape) that the parent
// already deletes with the rest of the screen's children; since the
// strip is added to the parent via add(), the parent's normal teardown
// frees it and the cursor members too.

#include "cross2d/c2d.h"

#include <functional>
#include <string>
#include <vector>

namespace cs::ui {

struct Chip {
    std::string label;       // "Uakino"
    std::string provider;    // "uakino"
    bool isEnabled = true;   // false → grayed-down (provider status == Down)
};

class ChipStrip : public c2d::RectangleShape {
public:
    using SelectCb = std::function<void(int index)>;

    ChipStrip(c2d::C2DRenderer *main, std::vector<Chip> chips, c2d::FloatRect pos);
    ~ChipStrip() override = default;

    // Move focus within the enabled chips. Disabled chips are skipped
    // over so Left/Right always lands on a selectable chip, even when
    // there are disabled chips in between.
    int moveLeft();
    int moveRight();

    // Fired when the user presses Cross on the focused chip. Returns
    // the focus index that was confirmed (so the parent can use it as
    // a fetch target). Disabled chips are rejected (no callback, no
    // return).
    int selectFocused();

    int currentIndex() const { return focusIndex_; }
    void setCurrentIndex(int idx);
    int chipCount() const { return static_cast<int>(chips_.size()); }
    const Chip &chipAt(int idx) const { return chips_.at(idx); }

    void setOnSelect(SelectCb cb) { onSelect_ = std::move(cb); }

    // Re-render chips, focus cursor, and labels. Call after mutating
    // chips_ or after a provider-status update changes isEnabled.
    void refresh();

    // True when at least one chip is enabled — callers use this to
    // decide whether to show the strip at all.
    bool hasEnabledChip() const;

private:
    // Find the next focus index in `dir` (-1 left, +1 right), skipping
    // disabled chips. Returns the new focus index, or focusIndex_ if
    // there are no enabled chips in the requested direction.
    int step(int dir);

    // Lay out chips on the strip and rewrite the per-chip Rectangle /
    // Text children. Called from refresh().
    void layout();

    std::vector<Chip> chips_;
    std::vector<c2d::RectangleShape *> chipBoxes_;
    std::vector<c2d::Text *> chipLabels_;
    c2d::RectangleShape *cursor_ = nullptr;
    c2d::C2DRenderer *main_ = nullptr;
    int focusIndex_ = 0;
    SelectCb onSelect_;
};

} // namespace cs::ui