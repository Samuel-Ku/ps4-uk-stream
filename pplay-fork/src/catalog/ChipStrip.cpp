#include "UiScale.h"
#include "ChipStrip.h"

#include "cross2d/skeleton/sfml/Color.hpp"

#include <algorithm>
#include <utility>

namespace cs::ui {

namespace {
constexpr int kChipFontSize = 22;
constexpr float kChipPaddingX = 16.0f;
constexpr float kChipPaddingY = 8.0f;
constexpr float kChipGap = 10.0f;

// Strip chrome colors. The same accent as the rest of the catalog so
// the chips read as the same UI family (issue #57, v3 spec §5.1).
const c2d::Color kChipFill = {0x12, 0x12, 0x12, 0xff};
const c2d::Color kChipBorder = {0x55, 0xef, 0xc4, 0xff};
const c2d::Color kChipText = {0xff, 0xff, 0xff, 0xff};
const c2d::Color kChipDisabledFill = {0x12, 0x12, 0x12, 0xff};
const c2d::Color kChipDisabledBorder = {0x55, 0x55, 0x55, 0xc0};
const c2d::Color kChipDisabledText = {0x88, 0x88, 0x88, 0xff};

const c2d::Color kFocusFill = {0x55, 0xef, 0xc4, 0x40};
const c2d::Color kFocusBorder = {0x55, 0xef, 0xc4, 0xff};
} // namespace

ChipStrip::ChipStrip(c2d::C2DRenderer *main, std::vector<Chip> chips, c2d::FloatRect pos)
    : RectangleShape({pos.left, pos.top, pos.width, pos.height}),
      chips_(std::move(chips)),
      main_(main) {
    // The shell is transparent — the chips own the visible geometry.
    setFillColor(c2d::Color{0, 0, 0, 0});
    setLayer(5);

    // Pick the first enabled chip as the initial focus. If none are
    // enabled, focusIndex_ stays at 0 — selectFocused() will reject
    // presses anyway.
    auto firstEnabled = std::find_if(chips_.begin(), chips_.end(),
                                     [](const Chip &c) { return c.isEnabled; });
    if (firstEnabled != chips_.end()) {
        focusIndex_ = static_cast<int>(firstEnabled - chips_.begin());
    }

    // Per-chip rectangles + text. We allocate the children here; layout()
    // below positions them. The parent screen owns the strip (and the
    // strip's children) via add() — same ownership pattern as every
    // other widget in the catalog.
    chipBoxes_.reserve(chips_.size());
    chipLabels_.reserve(chips_.size());
    for (size_t i = 0; i < chips_.size(); ++i) {
        auto *box = new c2d::RectangleShape({0, 0, 0, 0});
        box->setOutlineThickness(2.0f);
        add(box);
        chipBoxes_.push_back(box);

        auto *label = new c2d::Text(chips_[i].label, kChipFontSize, main_->getFont());
        add(label);
        chipLabels_.push_back(label);
    }

    cursor_ = new c2d::RectangleShape({0, 0, 0, 0});
    cursor_->setFillColor(kFocusFill);
    cursor_->setOutlineColor(kFocusBorder);
    cursor_->setOutlineThickness(kFocusOutline);
    add(cursor_);

    layout();
}

void ChipStrip::layout() {
    if (chips_.empty()) return;
    const float startX = getPosition().x;
    const float startY = getPosition().y;
    float x = startX;
    // Use the focused chip's height as the row height — we keep all
    // chips on a single line so the strip never wraps.
    for (size_t i = 0; i < chips_.size(); ++i) {
        const auto &label = chipLabels_[i];
        label->setFillColor(chips_[i].isEnabled ? kChipText : kChipDisabledText);
        const c2d::Vector2f labelSize = label->getSize();
        const float w = labelSize.x + 2.0f * kChipPaddingX;
        const float h = labelSize.y + 2.0f * kChipPaddingY;
        chipBoxes_[i]->setSize({w, h});
        chipBoxes_[i]->setPosition({x, startY});
        chipBoxes_[i]->setFillColor(chips_[i].isEnabled ? kChipFill : kChipDisabledFill);
        chipBoxes_[i]->setOutlineColor(chips_[i].isEnabled ? kChipBorder : kChipDisabledBorder);
        // Center the label vertically inside the chip box.
        label->setPosition({x + kChipPaddingX, startY + kChipPaddingY - 4.0f});
        x += w + kChipGap;
    }
    // Position the focus cursor on the focused chip with the standard
    // outline pad (v3 spec §5.1, issue #75 — math lives in UiScale.h).
    const auto &box = chipBoxes_[focusIndex_];
    const float padding = 4.0f;
    drawFocusBox(cursor_,
                 {box->getPosition().x - padding,
                  box->getPosition().y - padding,
                  box->getSize().x + 2.0f * padding,
                  box->getSize().y + 2.0f * padding},
                 kFocusOutline);
}

void ChipStrip::refresh() {
    layout();
}

int ChipStrip::step(int dir) {
    if (chips_.empty()) return 0;
    int idx = focusIndex_;
    for (size_t i = 0; i < chips_.size(); ++i) {
        idx = (idx + dir + static_cast<int>(chips_.size())) %
              static_cast<int>(chips_.size());
        if (chips_[idx].isEnabled) return idx;
    }
    return focusIndex_;  // no enabled chip in that direction
}

int ChipStrip::moveLeft() {
    focusIndex_ = step(-1);
    layout();
    return focusIndex_;
}

int ChipStrip::moveRight() {
    focusIndex_ = step(+1);
    layout();
    return focusIndex_;
}

void ChipStrip::setCurrentIndex(int idx) {
    if (chips_.empty()) return;
    if (idx < 0 || idx >= static_cast<int>(chips_.size())) return;
    focusIndex_ = idx;
    layout();
}

int ChipStrip::selectFocused() {
    if (chips_.empty()) return focusIndex_;
    if (!chips_[focusIndex_].isEnabled) return -1;
    if (onSelect_) onSelect_(focusIndex_);
    return focusIndex_;
}

bool ChipStrip::hasEnabledChip() const {
    for (const auto &c : chips_) {
        if (c.isEnabled) return true;
    }
    return false;
}

} // namespace cs::ui