#include "ScreenSections.h"

#include <cstdio>

namespace cs {

ScreenSections::ScreenSections(c2d::C2DRenderer *main)
    : RectangleShape({0, 0, static_cast<float>(main->getSize().x),
                      static_cast<float>(main->getSize().y)}) {
    // Solid dark background so the user sees something deliberate
    // (rather than the previous screen bleeding through).
    setFillColor(c2d::Color{0x12, 0x12, 0x12, 0xff});
    setOutlineColor(c2d::Color{0x55, 0xef, 0xc4, 0xff}); // mint accent
    setOutlineThickness(2.0f);

    // TODO: replace this placeholder with the real two-column layout
    // (providers <- left, sections -> right) plus a CatalogApi-backed
    // fetch on `onUpdate`. Tracked under plan Task 18.
    std::fprintf(stderr,
                 "cs::ScreenSections: placeholder shown — full Sections/Search/"
                 "Results/Content UI lands in Task 18.\n");
}

void ScreenSections::onUpdate() {
    RectangleShape::onUpdate();
}

} // namespace cs
