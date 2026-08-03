//
// Created by cpasjuste on 03/10/18.
//

#ifndef PPLAY_PLAYER_H
#define PPLAY_PLAYER_H

#include <functional>

#include "menus/menu_video_submenu.h"
#include "media_file.h"
#include "mpv.h"

class Main;

class PlayerOSD;

class VideoTexture;

class Player : public c2d::Rectangle {

public:

    explicit Player(Main *main);

    ~Player() override;

    bool load(const MediaFile &file);

    void pause();

    void resume();

    void stop();

    void setSpeed(double speed);

    bool isFullscreen();

    void setFullscreen(bool maximize, bool hide = false);

    void setVideoStream(int streamId);

    void setAudioStream(int streamId);

    void setSubtitleStream(int streamId);

    int getVideoStream();

    int getAudioStream();

    int getSubtitleStream();

    PlayerOSD *getOSD();

    Mpv *getMpv();

    MenuVideoSubmenu *getMenuVideoStreams();

    MenuVideoSubmenu *getMenuAudioStreams();

    MenuVideoSubmenu *getMenuSubtitlesStreams();

    const std::string &getTitle() const;

    // Catalog hook (issue #55): while set, called with (positionSec,
    // durationSec) roughly every 10 s of active playback, and once when
    // playback stops. Set by the catalog layer right before load();
    // cleared on stop. On PS4 there is no real position yet (mpv stub) —
    // the saver still fires and simply records zeros.
    void setPositionSaver(std::function<void(long positionSec, long durationSec)> saver);

    bool onInput(c2d::Input::Player *players) override;

private:

    void onUpdate() override;

    void onLoadEvent();

    void onStopEvent(int reason);

    // ui
    Main *main = nullptr;
    PlayerOSD *osd = nullptr;
    c2d::TweenScale *tweenScale = nullptr;
    c2d::TweenPosition *tweenPosition = nullptr;
    MenuVideoSubmenu *menuVideoStreams = nullptr;
    MenuVideoSubmenu *menuAudioStreams = nullptr;
    MenuVideoSubmenu *menuSubtitlesStreams = nullptr;
    MediaFile file;

    // player
    VideoTexture *texture = nullptr;
    Mpv *mpv;

    bool fullscreen = false;

    // catalog resume reporting (#55)
    std::function<void(long, long)> positionSaver_;
    float positionSaverElapsed_ = 0.0f;
};

#endif //PPLAY_PLAYER_H
