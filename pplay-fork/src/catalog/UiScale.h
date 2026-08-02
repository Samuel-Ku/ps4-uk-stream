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

// Focus highlight: thick outline + filled wash + a 1.05 center-anchored
// scale (v3 spec §5.1 requires BOTH the outline and the scale).
constexpr float kFocusOutline = 3.0f;
constexpr float kFocusScale = 1.05f;

// Center-anchored focus scale: grows the box (x, y, w, h) by kFocusScale
// about its center, in place. Scaling about the center is what makes the
// focus "no-jump" — the element enlarges rather than shifting.
inline void scaleFocus(float &x, float &y, float &w, float &h) {
    const float sw = w * kFocusScale;
    const float sh = h * kFocusScale;
    x += (w - sw) / 2.0f;
    y += (h - sh) / 2.0f;
    w = sw;
    h = sh;
}

// Results rows and poster thumbnails.
constexpr float kRowHeight = 72.0f;      // list row stride
constexpr float kPosterThumb = 144.0f;   // focused-item poster box side

// Keyboard grid cells.
constexpr float kKeyCellW = 72.0f;

} // namespace cs::ui
