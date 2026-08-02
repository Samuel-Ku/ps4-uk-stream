#pragma once

// Non-modal toast (issue #64, v3 spec §5.4).
//
// Anchored to the bottom-center of the parent renderer. Auto-dismisses
// after `kToastDurationSec` (~3 s). Never blocks input — the user can
// press buttons while a toast is on screen; the toast is purely a hint
// surface, not a modal dialog. NO automatic retries anywhere — toasts
// never trigger an upstream re-fetch.
//
// One toast at a time: `show()` replaces any toast currently on screen.
// The toast is rendered above the active screen (high z-order); the
// renderer is responsible for keeping it on top via setLayer().

#include "cross2d/c2d.h"

#include <chrono>
#include <string>

namespace cs::ui {

constexpr float kToastDurationSec = 3.0f;

class Toast : public c2d::RectangleShape {
public:
    explicit Toast(c2d::C2DRenderer *renderer);

    // Replace any visible toast with `message`. Auto-dismiss timer starts
    // from this call. Safe to call repeatedly; each call resets the timer.
    void show(const std::string &message);

    // Force-hide. Useful when navigating away from a screen.
    void dismiss();

    // Tick the auto-dismiss timer. Call from the active screen's onUpdate.
    void onUpdate() override;

    // Return true while the toast is on screen.
    bool visible() const { return visible_; }

private:
    void layout();

    c2d::C2DRenderer *renderer_ = nullptr;
    c2d::Text *text_ = nullptr;
    bool visible_ = false;
    float elapsed_ = 0.0f;
};

} // namespace cs::ui
