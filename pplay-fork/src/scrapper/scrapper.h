//
// Created by cpasjuste on 29/03/19.
//

#ifndef PPLAY_SCRAPPER_H
#define PPLAY_SCRAPPER_H

#include <string>
#include "cross2d/skeleton/sfml/RectangleShape.hpp"

class Main;

namespace pplay {

    class Scrapper {

    public:

        explicit Scrapper(Main *main);

        ~Scrapper();

        int scrap(const std::string &path);

        Main *main;
        std::string path;
#ifndef __PS4__
        // The threading primitives live in the Linux / Switch / Vita
        // builds of libcross2d. The PS4 build doesn't ship a scrapper
        // (libcurl + json-c aren't in OpenOrbisSDK), so the
        // pscrap-backed background worker doesn't exist on PS4.
        c2d::C2DMutex *mutex = nullptr;
        c2d::C2DCond *cond = nullptr;
        c2d::C2DThread *thread = nullptr;
#endif
        bool scrapping = false;
        bool running = true;
    };
}

#endif //PPLAY_SCRAPPER_H
