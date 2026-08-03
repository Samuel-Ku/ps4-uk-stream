#pragma once

#include "cross2d/skeleton/sfml/Rect.hpp"
#include "cross2d/skeleton/sfml/RectangleShape.hpp"

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

// One geometry type for the focus-highlight box. The framework's
// FloatRect (Rect<float>: left/top/width/height) is the natural fit —
// the cursor shapes and their position/size API are c2d types too, so
// no bespoke struct is needed.
using FocusBox = c2d::FloatRect;

// Center-anchored focus scale: grows the box by kFocusScale about its
// center. Scaling about the center is what makes the focus "no-jump" —
// the element enlarges rather than shifting.
inline FocusBox scaleFocus(FocusBox box) {
    const float sw = box.width * kFocusScale;
    const float sh = box.height * kFocusScale;
    box.left += (box.width - sw) / 2.0f;
    box.top += (box.height - sh) / 2.0f;
    box.width = sw;
    box.height = sh;
    return box;
}

// Renders the focus highlight on a cursor shape: applies the center-
// anchored 1.05 scale to the element box and stamps position/size onto
// the shape, re-applying the outline thickness so the outline geometry
// lives in one place. The box carries each screen's outline padding
// (e.g. -4/+8 in Results), which is what keeps the outline visible
// around the element while the shape grows.
inline void drawFocusBox(c2d::RectangleShape *cursor, const FocusBox &element,
                         float outlineWidth) {
    const FocusBox box = scaleFocus(element);
    cursor->setOutlineThickness(outlineWidth);
    cursor->setPosition({box.left, box.top});
    cursor->setSize({box.width, box.height});
}

// Results rows and poster thumbnails.
constexpr float kRowHeight = 72.0f;      // list row stride

// Keyboard grid cells.
constexpr float kKeyCellW = 72.0f;

} // namespace cs::ui
