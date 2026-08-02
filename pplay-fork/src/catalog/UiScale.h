#pragma once

// 10-foot UI constants for the catalog screens (issue #57, v3 spec §5.1).
// Catalog screens lay out in real pixels against main->getSize() (no
// 1280x720 scaling factor is applied to them), so these are anchored to
// the PS4's 1080p framebuffer.

namespace cs::ui {

// Typography floor — nothing in the catalog renders below kSmallSize.
constexpr int kSmallSize = 24;   // status lines, meta, hints
constexpr int kBodySize = 28;    // lists, descriptions, keyboard keys
constexpr int kTitleSize = 32;   // screen titles

// 5% action-safe margins on 1080p (96 = 5% of 1920, 54 = 5% of 1080).
constexpr float kMarginX = 96.0f;
constexpr float kMarginY = 54.0f;

// Inner spacing between panels/widgets (not screen-edge margins).
constexpr float kGap = 16.0f;

// Focus highlight: thick outline + filled wash (spec allows outline OR
// scale-1.05; outline chosen for text-list screens).
constexpr float kFocusOutline = 3.0f;

// Results rows and poster thumbnails.
constexpr float kRowHeight = 72.0f;      // list row stride
constexpr float kPosterThumb = 144.0f;   // focused-item poster box side

// Keyboard grid cells.
constexpr float kKeyCellW = 72.0f;

} // namespace cs::ui
