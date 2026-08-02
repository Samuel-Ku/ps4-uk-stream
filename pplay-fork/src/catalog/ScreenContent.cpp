#include "ScreenContent.h"
#include "CatalogContext.h"
#include "main.h"

#include "media_file.h"
#include "mpv.h"

#ifndef __PS4__
#include <mpv/client.h>
#endif

#include <cstdio>
#include <sstream>

namespace cs {

namespace {
constexpr int kTitleSize = 28;
constexpr int kMetaSize = 18;
constexpr int kBodySize = 18;
constexpr int kStatusSize = 16;
constexpr float kPanelPadding = 16.0f;
constexpr size_t kDescMaxChars = 320;

const char *typeLabel(const std::string &type) {
    if (type == "movie") return "Фільм";
    if (type == "series") return "Серіал";
    if (type == "anime") return "Аніме";
    if (type == "cartoon") return "Мультфільм";
    if (type == "dorama") return "Дорама";
    return "?";
}

// Apply HTTP headers to the running mpv instance. mpv takes headers as
// a comma-separated list of `Key: Value` pairs (the same format that
// `curl -H` and the gate.sh `headers_mpv` helper produce). Setting
// http-header-fields before `loadfile` ensures the next stream request
// includes them — that is what Uakino and most uk providers need for
// Referer / User-Agent to allow the CDN access.
void applyMpvHeaders(class Mpv *mpv,
                     const std::vector<std::pair<std::string, std::string>> &headers) {
    if (!mpv || headers.empty()) return;
    std::ostringstream oss;
    for (size_t i = 0; i < headers.size(); ++i) {
        if (i > 0) oss << ",";
        oss << headers[i].first << ": " << headers[i].second;
    }
    const std::string cmd = "set http-header-fields " + oss.str();
    mpv_command_string(mpv->getHandle(), cmd.c_str());
}
} // namespace

ScreenContent::ScreenContent(c2d::C2DRenderer *main, std::string id, std::string title)
    : RectangleShape({0, 0, static_cast<float>(static_cast<c2d::C2DRenderer *>(main)->getSize().x),
                      static_cast<float>(static_cast<c2d::C2DRenderer *>(main)->getSize().y)}),
      api_(CatalogContext::get()),
      main_(static_cast<Main *>(main)),
      id_(std::move(id)),
      pendingTitle_(std::move(title)) {
    setFillColor(c2d::Color{0x12, 0x12, 0x12, 0xff});
    setLayer(5);

    title_ = new c2d::Text(pendingTitle_, kTitleSize, main_->getFont());
    title_->setPosition({kPanelPadding, kPanelPadding});
    title_->setFillColor(c2d::Color{0xff, 0xff, 0xff, 0xff});
    add(title_);

    meta_ = new c2d::Text("", kMetaSize, main_->getFont());
    meta_->setPosition({kPanelPadding, kPanelPadding + kTitleSize + 8});
    meta_->setFillColor(c2d::Color{0xaa, 0xaa, 0xaa, 0xff});
    add(meta_);

    description_ = new c2d::Text("", kBodySize, main_->getFont());
    description_->setPosition({kPanelPadding, kPanelPadding + kTitleSize + kMetaSize + 16});
    description_->setFillColor(c2d::Color{0xdd, 0xdd, 0xdd, 0xff});
    add(description_);

    translationsLabel_ = new c2d::Text("", kBodySize, main_->getFont());
    translationsLabel_->setPosition({kPanelPadding, description_->getPosition().y + kBodySize * 8 + 16});
    translationsLabel_->setFillColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
    add(translationsLabel_);

    seasonsLabel_ = new c2d::Text("", kBodySize, main_->getFont());
    seasonsLabel_->setPosition({kPanelPadding, translationsLabel_->getPosition().y + kBodySize + 8});
    seasonsLabel_->setFillColor(c2d::Color{0xaa, 0xaa, 0xaa, 0xff});
    add(seasonsLabel_);

    episodesLabel_ = new c2d::Text("", kBodySize, main_->getFont());
    episodesLabel_->setPosition({kPanelPadding, seasonsLabel_->getPosition().y + kBodySize + 16});
    episodesLabel_->setFillColor(c2d::Color{0xee, 0xee, 0xee, 0xff});
    add(episodesLabel_);

    cursor_ = new c2d::RectangleShape({0, 0, 0, 0});
    cursor_->setFillColor(c2d::Color{0x55, 0xef, 0xc4, 0x40});
    cursor_->setOutlineColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
    cursor_->setOutlineThickness(1.5f);
    add(cursor_);

    status_ = new c2d::Text("", kStatusSize, main_->getFont());
    status_->setPosition({kPanelPadding, static_cast<float>(static_cast<c2d::C2DRenderer *>(main_)->getSize().y) - kStatusSize - kPanelPadding});
    status_->setFillColor(c2d::Color{0xaa, 0xaa, 0xaa, 0xff});
    add(status_);

    setStatus("Завантаження…");
    requestContent();
}

std::string ScreenContent::wrapDescription(const std::string &desc, size_t maxLine) {
    if (desc.size() <= maxLine) return desc;
    return desc.substr(0, maxLine) + "…";
}

void ScreenContent::setStatus(const std::string &s) {
    if (status_) status_->setString(s);
}

void ScreenContent::requestContent() {
    if (!api_) { setStatus("Backend недоступний"); return; }
    contentFetched_.store(false, std::memory_order_release);
    api_->contentAsync(id_, [this](bool ok, ContentItem it, std::string err) {
        if (ok) {
            fetchedItem_ = std::move(it);
            fetchError_.clear();
        } else {
            fetchedItem_ = ContentItem{};
            fetchError_ = err.empty() ? "невідома помилка" : err;
        }
        contentFetched_.store(true, std::memory_order_release);
    });
}

void ScreenContent::renderAll() {
    if (!title_->getString().size() && !pendingTitle_.empty()) {
        // Keep the title the caller passed in until the api response lands.
    }
    if (!item_.title.empty()) title_->setString(item_.title);

    std::ostringstream m;
    m << typeLabel(item_.type);
    if (!item_.id.empty()) m << " · " << item_.id;
    meta_->setString(m.str());

    description_->setString(wrapDescription(item_.description, kDescMaxChars));

    if (item_.translationsLevel == "content") {
        std::ostringstream t;
        t << "Переклад: ";
        for (size_t i = 0; i < item_.translations.size(); ++i) {
            if (i > 0) t << ", ";
            t << item_.translations[i].second;
        }
        if (item_.translations.empty()) t << "(немає)";
        translationsLabel_->setString(t.str());
    } else {
        translationsLabel_->setString("");
    }

    std::ostringstream s;
    s << "Сезони: ";
    for (size_t i = 0; i < item_.seasons.size(); ++i) {
        if (i > 0) s << "  ";
        if (i == static_cast<size_t>(seasonIndex_)) s << "[";
        s << "S" << item_.seasons[i].number;
        if (i == static_cast<size_t>(seasonIndex_)) s << "]";
    }
    if (item_.seasons.empty()) s << "(немає)";
    seasonsLabel_->setString(s.str());

    std::ostringstream e;
    if (seasonIndex_ >= 0 && seasonIndex_ < static_cast<int>(item_.seasons.size())) {
        const auto &episodes = item_.seasons[seasonIndex_].episodes;
        for (size_t i = 0; i < episodes.size(); ++i) {
            if (i > 0) e << "\n";
            if (i == static_cast<size_t>(episodeIndex_)) e << "> ";
            else e << "  ";
            e << "E" << episodes[i].number;
            if (!episodes[i].title.empty()) e << " · " << episodes[i].title;
            if (item_.translationsLevel == "episode" &&
                !episodes[i].translations.empty()) {
                e << " [";
                for (size_t j = 0; j < episodes[i].translations.size(); ++j) {
                    if (j > 0) e << ",";
                    e << episodes[i].translations[j].second;
                }
                e << "]";
            }
        }
        if (episodes.empty()) e << "(немає епізодів)";
    } else {
        e << "(фільм)";
    }
    episodesLabel_->setString(e.str());

    const float epY = episodesLabel_->getPosition().y;
    cursor_->setPosition({kPanelPadding - 4, epY + episodeIndex_ * (kBodySize + 4)});
    cursor_->setSize({episodesLabel_->getSize().x + 8, kBodySize + 4});
}

void ScreenContent::playEpisode(int seasonIdx, int epIdx, const std::string &translationId) {
    if (!api_) return;
    streamFetched_.store(false, std::memory_order_release);
    setStatus("Отримую URL…");
    std::string epId;
    std::string epTitle;
    if (seasonIdx >= 0 && seasonIdx < static_cast<int>(item_.seasons.size()) &&
        epIdx >= 0 && epIdx < static_cast<int>(item_.seasons[seasonIdx].episodes.size())) {
        epId = item_.seasons[seasonIdx].episodes[epIdx].id;
        epTitle = item_.seasons[seasonIdx].episodes[epIdx].title;
    } else {
        epId = id_;
        epTitle = pendingTitle_;
    }
    streamTitle_ = epTitle;
    api_->streamAsync(epId, translationId,
        [this](bool ok, StreamInfo info, std::string err) {
            if (ok) {
                streamUrl_ = std::move(info.url);
                streamHeaders_ = std::move(info.headers);
                streamError_.clear();
            } else {
                streamUrl_.clear();
                streamHeaders_.clear();
                streamError_ = err.empty() ? "невідома помилка" : err;
            }
            streamFetched_.store(true, std::memory_order_release);
        });
}

void ScreenContent::onUpdate() {
    // ---- content fetch (single pull) ----
    if (contentFetched_.load(std::memory_order_acquire)) {
        contentFetched_.store(false, std::memory_order_release);
        item_ = std::move(fetchedItem_);
        seasonIndex_ = 0;
        episodeIndex_ = 0;
        episodeTranslationIndex_ = 0;
        renderAll();
        if (!fetchError_.empty()) {
            setStatus("Помилка: " + fetchError_);
        } else if (item_.seasons.empty()) {
            setStatus("Готово · Фільм — A: грати");
        } else {
            setStatus("Готово · A: грати епізод");
        }
    }

    // ---- stream hand-off (single pull) ----
    if (streamFetched_.load(std::memory_order_acquire)) {
        streamFetched_.store(false, std::memory_order_release);
        if (!streamError_.empty() || streamUrl_.empty()) {
            setStatus("Помилка потоку: " + (streamError_.empty() ? "немає URL" : streamError_));
        } else {
            auto *player = main_->getPlayer();
            if (!player) {
                setStatus("Player недоступний");
            } else {
                applyMpvHeaders(player->getMpv(), streamHeaders_);
                c2d::Io::File f;
                f.name = streamTitle_;
                f.path = streamUrl_;
                f.type = c2d::Io::Type::File;
                MediaFile mf;
                mf.name = f.name;
                mf.path = f.path;
                mf.type = f.type;
                player->load(mf);
                setVisibility(c2d::Visibility::Hidden, true);
                return;
            }
        }
    }

    if (!item_.id.empty() || !fetchedItem_.id.empty()) {
        // Ready for input — fall through.
    } else if (!contentFetched_.load(std::memory_order_acquire)) {
        RectangleShape::onUpdate();
        return;
    }

    const unsigned int keys = main_->getInput()->getKeys(0);
    const int seasonCount = static_cast<int>(item_.seasons.size());
    const int epCount = (seasonIndex_ >= 0 && seasonIndex_ < seasonCount)
        ? static_cast<int>(item_.seasons[seasonIndex_].episodes.size()) : 0;

    if (keys & c2d::Input::Key::Left) {
        if (seasonCount > 0) {
            seasonIndex_ = (seasonIndex_ - 1 + seasonCount) % seasonCount;
            episodeIndex_ = 0;
            renderAll();
        }
    } else if (keys & c2d::Input::Key::Right) {
        if (seasonCount > 0) {
            seasonIndex_ = (seasonIndex_ + 1) % seasonCount;
            episodeIndex_ = 0;
            renderAll();
        }
    } else if (keys & c2d::Input::Key::Up) {
        if (epCount > 0) {
            episodeIndex_ = (episodeIndex_ - 1 + epCount) % epCount;
            episodeTranslationIndex_ = 0;
            renderAll();
        }
    } else if (keys & c2d::Input::Key::Down) {
        if (epCount > 0) {
            episodeIndex_ = (episodeIndex_ + 1) % epCount;
            episodeTranslationIndex_ = 0;
            renderAll();
        }
    } else if (keys & c2d::Input::Key::Fire1) {
        std::string translationId;
        if (item_.translationsLevel == "episode" && epCount > 0) {
            const auto &ep = item_.seasons[seasonIndex_].episodes[episodeIndex_];
            if (!ep.translations.empty()) {
                const int idx = episodeTranslationIndex_ %
                    static_cast<int>(ep.translations.size());
                translationId = ep.translations[idx].first;
            }
        } else if (!item_.translations.empty()) {
            translationId = item_.translations[0].first;
        }
        playEpisode(seasonIndex_, episodeIndex_, translationId);
    } else if (keys & c2d::Input::Key::Fire3) {
        // Y — cycle episode translation (episode-level only)
        if (item_.translationsLevel == "episode" && epCount > 0) {
            const auto &trs = item_.seasons[seasonIndex_].episodes[episodeIndex_].translations;
            if (!trs.empty()) {
                episodeTranslationIndex_ = (episodeTranslationIndex_ + 1) %
                    static_cast<int>(trs.size());
                renderAll();
            }
        }
    } else if (keys & c2d::Input::Key::Fire2) {
        setVisibility(c2d::Visibility::Hidden, true);
    }

    RectangleShape::onUpdate();
}

} // namespace cs