#pragma once

#include "cross2d/c2d.h"

namespace cs {

// Placeholder "Sections" screen — the first screen the user sees after
// selecting "Каталог UA" from the main menu.
//
// The full Sections/Search/Results/Content screens (with providers,
// section list, on-screen keyboard, posters, and player hand-off)
// are added in the next plan pass. Until then this screen compiles
// and links, and gives the user a blank-but-renderable surface so
// the menu entry does not crash the build.
//
// Usage:
//   push(new ScreenSections(this));
class ScreenSections : public c2d::RectangleShape {
public:
    explicit ScreenSections(c2d::C2DRenderer *main);
    ~ScreenSections() override = default;

    void onUpdate() override;
};

} // namespace cs
