#include "UiScale.h"
#include "ScreenContent.h"
#include "CatalogContext.h"
#include "ErrorStrings.h"
#include "ChipStrip.h"
#include "main.h"

#include "media_file.h"
#include "mpv.h"

#ifndef __PS4__
#include <mpv/client.h>
#endif

#include <cstdio>
#include <ctime>
#include <sstream>
#include <unordered_map>

namespace cs {

namespace {
// 10-foot scale: typography floor and action-safe margins anchored to
// 1080p (issue #57, v3 spec §5.1).
using ui::kSmallSize;
using ui::kBodySize;
using ui::kTitleSize;
using ui::kMarginX;
using ui::kMarginY;
using ui::kFocusOutline;
using ui::drawFocusBox;
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
    title_->setPosition({kMarginX, kMarginY});
    title_->setFillColor(c2d::Color{0xff, 0xff, 0xff, 0xff});
    add(title_);

    meta_ = new c2d::Text("", kSmallSize, main_->getFont());
    meta_->setPosition({kMarginX, kMarginY + kTitleSize + 8});
    meta_->setFillColor(c2d::Color{0xaa, 0xaa, 0xaa, 0xff});
    add(meta_);

    description_ = new c2d::Text("", kBodySize, main_->getFont());
    description_->setPosition({kMarginX, kMarginY + kTitleSize + kSmallSize + 16});
    description_->setFillColor(c2d::Color{0xdd, 0xdd, 0xdd, 0xff});
    add(description_);

    translationsLabel_ = new c2d::Text("", kBodySize, main_->getFont());
    translationsLabel_->setPosition({kMarginX, description_->getPosition().y + kBodySize * 8 + 16});
    translationsLabel_->setFillColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
    add(translationsLabel_);

    seasonsLabel_ = new c2d::Text("", kBodySize, main_->getFont());
    seasonsLabel_->setPosition({kMarginX, translationsLabel_->getPosition().y + kBodySize + 8});
    seasonsLabel_->setFillColor(c2d::Color{0xaa, 0xaa, 0xaa, 0xff});
    add(seasonsLabel_);

    episodesLabel_ = new c2d::Text("", kBodySize, main_->getFont());
    episodesLabel_->setPosition({kMarginX, seasonsLabel_->getPosition().y + kBodySize + 16});
    episodesLabel_->setFillColor(c2d::Color{0xee, 0xee, 0xee, 0xff});
    add(episodesLabel_);

    cursor_ = new c2d::RectangleShape({0, 0, 0, 0});
    cursor_->setFillColor(c2d::Color{0x55, 0xef, 0xc4, 0x40});
    cursor_->setOutlineColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
    cursor_->setOutlineThickness(kFocusOutline);
    add(cursor_);

    // Issue #72 — "▶ Поновити з MM:SS" banner. Hidden by default; the
    // banner becomes visible once renderResumeBanner() finds a live
    // resume entry for this groupKey. The widget sits below the chip
    // strip; we position it lazily in renderResumeBanner() so it lands
    // exactly where the chip strip ends, regardless of font metrics.
    resumeBanner_ = new c2d::Text("", kBodySize, main_->getFont());
    resumeBanner_->setFillColor(c2d::Color{0x55, 0xef, 0xc4, 0xff});
    resumeBanner_->setVisibility(c2d::Visibility::Hidden);
    add(resumeBanner_);

    status_ = new c2d::Text("", kSmallSize, main_->getFont());
    status_->setPosition({kMarginX, static_cast<float>(static_cast<c2d::C2DRenderer *>(main_)->getSize().y) - kSmallSize - kMarginY});
    status_->setFillColor(c2d::Color{0xaa, 0xaa, 0xaa, 0xff});
    add(status_);

    setStatus("Завантаження…");
    requestContent();
}

void ScreenContent::rebuildChipStrip() {
    // Tear down the previous strip (if any) so we never leak its
    // children. The strip owns chip rectangles + labels + a focus cursor
    // — every one of those is freed by `delete strip` since the parent
    // owns the chain via add().
    if (chipStrip_ != nullptr) {
        remove(chipStrip_);
        delete chipStrip_;
        chipStrip_ = nullptr;
    }

    // Build chip model from item_.sources. Providers we know are DOWN
    // get grayed-down (visible but unselectable). Unknown / Up providers
    // are enabled. The single-chip case still renders — the user sees
    // which source this is — but the strip is degenerate (no Left/Right
    // movement). Issue #73: append a status hint so the user sees WHY
    // a chip is disabled (Down → "● Down"; Degraded → "⚠").
    std::vector<ui::Chip> chips;
    chips.reserve(item_.sources.size());
    for (const auto &s : item_.sources) {
        ui::Chip c;
        c.label = s.provider;
        c.provider = s.provider;
        const auto status = CatalogContext::providerStatus(s.provider);
        if (status == CatalogContext::ProviderStatus::Down) {
            c.isEnabled = false;
            c.statusHint = "● Down";
        } else if (status == CatalogContext::ProviderStatus::Degraded) {
            c.statusHint = "⚠";
        }
        chips.push_back(std::move(c));
    }

    if (chips.empty()) return;  // No source roster — refuse to render.

    const float W = static_cast<float>(static_cast<c2d::C2DRenderer *>(main_)->getSize().x);
    const float chipY = meta_->getPosition().y + kSmallSize + 8;
    chipStrip_ = new ui::ChipStrip(main_, chips, {kMarginX, chipY, W - 2 * kMarginX, 48.0f});
    add(chipStrip_);

    // Strip returns to the action row whenever we rebuild it — the
    // user just landed on this screen and the «Дивитись» row is the
    // primary affordance.
    focusActionRow();
}

void ScreenContent::focusActionRow() {
    focusMode_ = FocusMode::Action;
    cursor_->setVisibility(c2d::Visibility::Visible);
    if (chipStrip_) chipStrip_->setVisibility(c2d::Visibility::Hidden);
}

void ScreenContent::renderResumeBanner() {
    // Issue #72 — show "▶ Поновити з MM:SS" when this group has a
    // resume entry AND the entry is not finished (>= 95% per
    // CatalogState::isFinished). Memory is keyed by groupKey so the
    // entry is consistent across providers and chip switches.
    if (resumeBanner_ == nullptr) return;
    auto *state = CatalogContext::state();
    if (state == nullptr) {
        resumeBanner_->setVisibility(c2d::Visibility::Hidden);
        return;
    }
    const cs::ResumeEntry *e = state->resume(groupKey_);
    if (e == nullptr || e->positionSec <= 0 ||
        cs::CatalogState::isFinished(*e)) {
        resumeBanner_->setVisibility(c2d::Visibility::Hidden);
        return;
    }
    const long mins = e->positionSec / 60;
    const long secs = e->positionSec % 60;
    char buf[64];
    std::snprintf(buf, sizeof(buf), "▶ Поновити з %02ld:%02ld", mins, secs);
    resumeBanner_->setString(buf);
    // Position below the chip strip with a small gap. The strip's
    // height is 48 px; the chipY is meta_->y + kSmallSize + 8.
    float y = chipStrip_ != nullptr
                  ? chipStrip_->getPosition().y + 48.0f + 8.0f
                  : kMarginY + kTitleSize + kSmallSize + 8 + 48.0f + 8.0f;
    resumeBanner_->setPosition({kMarginX, y});
    resumeBanner_->setVisibility(c2d::Visibility::Visible);
}

void ScreenContent::focusChipStrip() {
    if (chipStrip_ == nullptr) return;
    focusMode_ = FocusMode::Chips;
    cursor_->setVisibility(c2d::Visibility::Hidden);
    chipStrip_->setVisibility(c2d::Visibility::Visible);
}

void ScreenContent::applyMemoryPreFocus() {
    // Issue #67 — movies are not remembered (one-shot). The store
    // never contains a movie entry; the check is defensive in case
    // a stale record from an earlier schema survives.
    if (!shouldRememberMemory(item_)) return;
    auto *state = CatalogContext::state();
    if (state == nullptr) return;
    const MemoryEntry *mem = state->memory(groupKey_);
    if (mem == nullptr) return;
    // Pre-focus the chip strip on the remembered provider, falling
    // through to the next enabled chip when the remembered one is
    // currently DOWN. Disabled chips are skipped by the strip's
    // setCurrentIndex path — it does NOT validate isEnabled, so we
    // walk manually to honor the fall-through rule.
    if (chipStrip_ != nullptr && !item_.sources.empty()) {
        int target = -1;
        for (size_t i = 0; i < item_.sources.size(); ++i) {
            if (item_.sources[i].provider != mem->provider) continue;
            const auto status = CatalogContext::providerStatus(item_.sources[i].provider);
            if (status == CatalogContext::ProviderStatus::Down) continue;
            target = static_cast<int>(i);
            break;
        }
        if (target < 0) {
            // Remembered provider is down — fall through to the first
            // enabled chip. The strip already skipped disabled chips
            // via its constructor focus-index picker, so any chip at
            // index 0 (or wherever focus landed on rebuild) is
            // enabled. setCurrentIndex(0) is a safe reset; the user
            // sees a healthy chip focused.
            for (size_t i = 0; i < item_.sources.size(); ++i) {
                if (CatalogContext::providerStatus(item_.sources[i].provider)
                    != CatalogContext::ProviderStatus::Down) {
                    target = static_cast<int>(i);
                    break;
                }
            }
        }
        if (target >= 0) chipStrip_->setCurrentIndex(target);
    }
    // Pre-focus the action row's translation index. Content-level
    // translations live on item_.translations as a vector of
    // {id, label} pairs (see CatalogApi parseContent); we match by
    // the LABEL (the second member) since translation ids are
    // provider-internal and may differ across sources. The pre-focus
    // is honored by the Fire1 handler when translationsLevel ==
    // "content" — Triangle cycles the episode translation otherwise.
    if (!item_.translations.empty()) {
        int match = -1;
        for (size_t i = 0; i < item_.translations.size(); ++i) {
            if (item_.translations[i].second == mem->translationLabel) {
                match = static_cast<int>(i);
                break;
            }
        }
        if (match >= 0) contentTranslationIndex_ = match;
    }
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
            // Issue #64: route backend error codes through humanError() so
            // raw snake_case never reaches the screen. The status string
            // uses the mapped UA phrase; the source code is dropped.
            fetchError_ = cs::ui::humanErrorOrGeneric(err);
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

    // When the chip strip is present, the description / translations /
    // seasons / episodes labels shift down by the strip's height + gap
    // so the strip has clean breathing room (issue #62 / v3 spec §5.1).
    // The chip strip is built after the content fetch lands, so this
    // branch is taken on the second renderAll() call (the first one
    // runs before the strip exists, with the original layout).
    const float chipStripHeight = chipStrip_ ? chipStrip_->getSize().y + 8.0f : 0.0f;
    description_->setPosition({kMarginX,
                               meta_->getPosition().y + kSmallSize + 16 + chipStripHeight});
    description_->setString(wrapDescription(item_.description, kDescMaxChars));

    if (item_.translationsLevel == "content") {
        std::ostringstream t;
        t << "Переклад: ";
        for (size_t i = 0; i < item_.translations.size(); ++i) {
            if (i > 0) t << ", ";
            // Mark the pre-selected translation with «▶» so the user
            // sees which dub will play on Cross (issue #67). The
            // index defaults to 0 on first open; memory pre-focus
            // (applyMemoryPreFocus) bumps it to the remembered label.
            if (static_cast<int>(i) == contentTranslationIndex_) t << "▶ ";
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
    // Cursor scaled 1.05 about its center on top of the outline
    // (v3 spec §5.1, issue #75; math lives in UiScale.h).
    drawFocusBox(cursor_, {kMarginX - 4, epY + episodeIndex_ * (kBodySize + 4),
                           episodesLabel_->getSize().x + 8, kBodySize + 4},
                 kFocusOutline);
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

    // Capture play context for the #55 store.
    pendingPlayEpisodeId_ = epId;
    pendingPlayTranslationLabel_.clear();
    if (seasonIdx >= 0 && seasonIdx < static_cast<int>(item_.seasons.size()) &&
        epIdx >= 0 && epIdx < static_cast<int>(item_.seasons[seasonIdx].episodes.size())) {
        for (const auto &t : item_.seasons[seasonIdx].episodes[epIdx].translations) {
            if (t.first == translationId) pendingPlayTranslationLabel_ = t.second;
        }
    }
    if (pendingPlayTranslationLabel_.empty()) {
        for (const auto &t : item_.translations) {
            if (t.first == translationId) pendingPlayTranslationLabel_ = t.second;
        }
    }

    api_->streamAsync(epId, translationId,
        [this](bool ok, StreamInfo info, std::string err) {
            if (ok) {
                streamUrl_ = std::move(info.url);
                streamHeaders_ = std::move(info.headers);
                streamError_.clear();
            } else {
                streamUrl_.clear();
                streamHeaders_.clear();
                // Issue #64: human UA string for every error surface.
                streamError_ = cs::ui::humanErrorOrGeneric(err);
            }
            streamFetched_.store(true, std::memory_order_release);
        });
}

void ScreenContent::onUpdate() {
    // ---- content fetch (single pull) ----
    if (contentFetched_.load(std::memory_order_acquire)) {
        contentFetched_.store(false, std::memory_order_release);
        item_ = std::move(fetchedItem_);
        // Capture the group key so chip-switch refetches can use the
        // group-aware endpoint. The screen's id_ tracks the active
        // source's content id; the group key is the cross-provider
        // identity (issue #69).
        if (item_.groupKey.empty()) {
            groupKey_ = item_.id;
        } else {
            groupKey_ = item_.groupKey;
        }
        seasonIndex_ = 0;
        episodeIndex_ = 0;
        episodeTranslationIndex_ = 0;
        contentTranslationIndex_ = 0;
        // Rebuild the chip strip after every content fetch — the strip
        // depends on item_.sources which only lands once the backend
        // responds. Rebuilding on each fetch also handles chip-switch
        // refetches: the new content has a new id_/sources, and the
        // strip needs to be rebuilt with the new roster.
        rebuildChipStrip();
        // Issue #67 — apply source/dub memory now that the chip strip
        // exists and we know whether the content has seasons. The
        // helper is a no-op for movies (no memory record exists) and
        // when no memory record is present.
        applyMemoryPreFocus();
        // Issue #72 — show "▶ Поновити з MM:SS" below the chip strip
        // when this group has a live resume entry. Hidden for finished
        // entries (>= 95%) and movies.
        renderResumeBanner();
        renderAll();

        // Issue #73 — refresh the provider health snapshot on every
        // group load. The chip strip's grayed-down / degraded flags
        // are driven by CatalogContext::providerStatus, which is
        // populated by this call. The strip is rebuilt below if the
        // snapshot lands between strip builds — we keep this fire-
        // and-forget so the user never waits on a health fetch.
        if (api_) {
            api_->providersAsync([this](bool ok, std::vector<ProviderInfo> providers,
                                        std::string err) {
                if (!ok) return;
                std::unordered_map<std::string, std::string> snapshot;
                for (const auto &p : providers) {
                    if (!p.id.empty()) snapshot[p.id] = p.status;
                }
                CatalogContext::setProviderStatuses(std::move(snapshot));
                // Rebuild the strip so the new statuses take effect
                // visually. The rebuild is cheap (per-allocation reflow).
                rebuildChipStrip();
                renderAll();
            });
        }
        if (!fetchError_.empty()) {
            setStatus("Помилка: " + fetchError_);
        } else if (item_.seasons.empty()) {
            setStatus("Готово · Фільм — X: грати");
        } else {
            setStatus("Готово · X: грати епізод");
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

                // v3 (issue #55): record what is playing so resume +
                // source/dub memory survive restarts. Records anchor on
                // the item's stateless group key (issue #69) so they stay
                // valid across sessions and provider-set changes; the id
                // is only a fallback for backends without the field.
                if (auto *st = CatalogContext::state()) {
                    ResumeEntry base;
                    base.groupKey = item_.groupKey.empty() ? item_.id : item_.groupKey;
                    base.provider = item_.id.substr(0, item_.id.find(':'));
                    base.id = item_.id;
                    if (!item_.seasons.empty()) base.episodeId = pendingPlayEpisodeId_;
                    base.translationLabel = pendingPlayTranslationLabel_;
                    base.updatedAt = static_cast<std::int64_t>(std::time(nullptr));
                    st->setResume(base);
                    // Series-form content is remembered; movies never are.
                    // Form = seasons presence (issue #74), not the type
                    // string — anime/cartoon/dorama are STYLES, and an
                    // anime movie has no seasons.
                    if (shouldRememberMemory(item_)) {
                        MemoryEntry m;
                        m.groupKey = base.groupKey;
                        m.provider = base.provider;
                        m.translationLabel = base.translationLabel;
                        m.updatedAt = base.updatedAt;
                        st->setMemory(m);
                    }
                    st->save();
                    player->setPositionSaver([base](long pos, long dur) mutable {
                        auto *state = CatalogContext::state();
                        if (!state) return;
                        base.positionSec = pos;
                        base.durationSec = dur;
                        base.updatedAt = static_cast<std::int64_t>(std::time(nullptr));
                        state->setResume(base);
                        state->save();
                    });
                }

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

    if (focusMode_ == FocusMode::Chips) {
        // Chip strip owns input. L/R moves focus, Cross fires the
        // focused chip (refetch the same group under that provider),
        // Triangle or Circle returns to Action row.
        if (keys & c2d::Input::Key::Left) {
            if (chipStrip_) chipStrip_->moveLeft();
        } else if (keys & c2d::Input::Key::Right) {
            if (chipStrip_) chipStrip_->moveRight();
        } else if (keys & c2d::Input::Key::Fire1) {
            if (chipStrip_ && chipStrip_->hasEnabledChip()) {
                const int idx = chipStrip_->selectFocused();
                if (idx >= 0 && idx < static_cast<int>(item_.sources.size())) {
                    const auto &src = item_.sources[idx];
                    if (src.id != id_) {
                        // Switch to the new source: refetch content under
                        // the same group key. The new id_ is the source's
                        // content id; groupKey_ stays the same.
                        id_ = src.id;
                        setStatus("Завантаження…");
                        contentFetched_.store(false, std::memory_order_release);
                        api_->contentAsyncForSource(groupKey_, src.provider,
                            [this](bool ok, ContentItem it, std::string err) {
                                if (ok) {
                                    fetchedItem_ = std::move(it);
                                    fetchError_.clear();
                                } else {
                                    fetchedItem_ = ContentItem{};
                                    fetchError_ = cs::ui::humanErrorOrGeneric(err);
                                }
                                contentFetched_.store(true, std::memory_order_release);
                            });
                    }
                }
                focusActionRow();
            }
        } else if (keys & c2d::Input::Key::Fire3 || keys & c2d::Input::Key::Fire2) {
            focusActionRow();
        }
    } else {
        // Action row (default focus). L/R cycles seasons, Up/Down cycles
        // episodes, Cross plays, Triangle opens the chip strip (when one
        // is rendered). Cycle-translation on Triangle when the chip
        // strip is absent so the existing (single-source) flow keeps
        // its Triangle-cycles-translation behavior.
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
                const int idx = contentTranslationIndex_ %
                    static_cast<int>(item_.translations.size());
                translationId = item_.translations[idx].first;
            }
            playEpisode(seasonIndex_, episodeIndex_, translationId);
        } else if (keys & c2d::Input::Key::Fire3) {
            // Triangle — open the chip strip when one is on screen.
            // Otherwise cycle the episode translation (legacy single-
            // source behavior).
            if (chipStrip_ && chipStrip_->hasEnabledChip()) {
                focusChipStrip();
            } else if (item_.translationsLevel == "episode" && epCount > 0) {
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
    }

    RectangleShape::onUpdate();
}

} // namespace cs