#include "Toast.h"
#include "UiScale.h"

namespace cs::ui {

Toast::Toast(c2d::C2DRenderer *renderer)
    : RectangleShape({0, 0, 0, 0}), renderer_(renderer) {
    using ui::kBodySize;
    using ui::kMarginX;
    using ui::kMarginY;

    const auto sz = renderer->getSize();
    const float w = static_cast<float>(sz.x);
    const float h = static_cast<float>(sz.y);

    // Visual surface: dark wash + thin border, anchored bottom-center.
    setFillColor(c2d::Color{0x20, 0x20, 0x20, 0xe0});
    setOutlineColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
    setOutlineThickness(2.0f);

    text_ = new c2d::Text("", kBodySize, renderer->getFont());
    text_->setFillColor(c2d::Color{0xff, 0xff, 0xff, 0xff});
    add(text_);

    // Initial layout: hidden, sized 0x0 at the bottom-center.
    setVisibility(c2d::Visibility::Hidden);
    setPosition({0, 0});
    setSize({0, 0});
    text_->setString("");

    // Sit on top of every screen.
    setLayer(50);

    // Anchor reference for layout (we keep this here so callers can find
    // the bottom-center anchor even when the toast is hidden).
    (void)w;
    (void)h;
    (void)kMarginX;
    (void)kMarginY;
}

void Toast::show(const std::string &message) {
    using ui::kBodySize;
    using ui::kMarginY;

    if (!text_ || !renderer_) return;
    text_->setString(message);

    // Measure the text, size the box around it (with 16 px inner padding).
    const auto txtSize = text_->getSize();
    const float padX = 16.0f;
    const float padY = 8.0f;
    const float boxW = static_cast<float>(txtSize.x) + padX * 2.0f;
    const float boxH = static_cast<float>(txtSize.y) + padY * 2.0f;

    const auto sz = renderer_->getSize();
    const float x = (static_cast<float>(sz.x) - boxW) / 2.0f;
    const float y = static_cast<float>(sz.y) - boxH - kMarginY;

    setPosition({x, y});
    setSize({boxW, boxH});
    text_->setPosition({x + padX, y + padY});

    visible_ = true;
    elapsed_ = 0.0f;
    setVisibility(c2d::Visibility::Visible);
}

void Toast::dismiss() {
    visible_ = false;
    elapsed_ = 0.0f;
    if (text_) text_->setString("");
    setVisibility(c2d::Visibility::Hidden);
}

void Toast::onUpdate() {
    if (!visible_) return;
    elapsed_ += renderer_->getDeltaTime().asSeconds();
    if (elapsed_ >= kToastDurationSec) {
        dismiss();
        return;
    }
    RectangleShape::onUpdate();
}

} // namespace cs::ui
