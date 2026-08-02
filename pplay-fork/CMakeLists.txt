cmake_minimum_required(VERSION 3.16)
#set(CMAKE_VERBOSE_MAKEFILE ON)

####################
# TARGET PLATFORM
####################
set(PLATFORM_LINUX OFF CACHE BOOL "Linux platform target")
set(PLATFORM_WINDOWS OFF CACHE BOOL "Windows platform target (msys/mingw64)")
set(PLATFORM_RPI3 OFF CACHE BOOL "RIP3 platform target")
set(PLATFORM_SWITCH OFF CACHE BOOL "Nintendo Switch platform target")
set(PLATFORM_3DS OFF CACHE BOOL "Nintendo 3DS platform target")
set(PLATFORM_VITA OFF CACHE BOOL "Sony PS Vita platform target")

####################
# BUILD OPTIONS
####################
set(OPTION_SMB2 OFF CACHE BOOL "Build with smb2 support")

###################
# MODULES
###################
add_subdirectory(libcross2d)
# pscrap is the standalone HTTP scraper backend client (uses libcurl +
# json-c). On PS4 we ship the streamer client only — the scraper runs
# as a separate Linux service out of band — so skip it for PS4 to
# avoid pulling in libcurl/json-c as cross-compile deps. Linking
# ${PROJECT_NAME} against `pscrap` below is also gated.
if (NOT PLATFORM_PS4)
    add_subdirectory(pscrap)
endif ()

##############
# pplay
##############
project(pplay)

# pplay version
set(VERSION_MAJOR "3")
set(VERSION_MINOR "7")
set(PROJECT_AUTHOR "Cpasjuste")
set(TITLE_ID "PPLAY0001")

# deps
set(CMAKE_MODULE_PATH ${CMAKE_MODULE_PATH} "${CMAKE_SOURCE_DIR}/cmake")
# BZip2 / MPV / ass are not needed for the streaming playback path
# the PS4 build is targeting. The upstream `REQUIRED` markers fail
# the configure step when the sysroot (OpenOrbisSDK) doesn't ship
# those libs. Skip them when targeting PS4.
if (NOT PLATFORM_PS4)
    find_package(BZip2 REQUIRED)
    find_package(MPV REQUIRED)
    find_package(ffmpeg COMPONENTS avfilter avcodec avformat avutil swscale swresample)
    find_package(ass REQUIRED)
else ()
    find_package(ffmpeg COMPONENTS avfilter avcodec avformat avutil swscale swresample)
endif ()

# sources
file(GLOB PPLAY_SRC
        src/*.c*
        src/catalog/*.c*
        src/filer/*.c*
        src/filer/Browser/*.c*
        src/filer/ftplib/*.c*
        src/menus/*.c*
        src/player/*.c*
        external/cJSON/cJSON.c
        )
# Scrapper is the TMDB / IMDB metadata fetch wired into the catalog
# browser UI. It uses libcurl + json-c and the OpenOrbisSDK doesn't
# ship either, so on PS4 we ship the catalog client only — the actual
# scrapper runs as a separate Linux service out of band. A no-op
# `pplay::Scrapper` stub lives at src/scrapper/scrapper_stub.cpp to
# satisfy the `scrapper` member in Main.
if (PLATFORM_PS4)
    list(APPEND PPLAY_SRC src/scrapper/scrapper_stub.cpp)
    # ps4_stubs/mpv_ps4_vars.cpp defines ps4_mpv_use_precompiled_shaders
    # and ps4_mpv_dump_shaders (replacing the symbols libmpv ships in
    # its PS4 platform layer). Without this, the link fails with
    # `undefined symbol: ps4_mpv_use_precompiled_shaders`. See
    # ps4_stubs/mpv_stub.h for the declaration.
    list(APPEND PPLAY_SRC src/player/ps4_stubs/mpv_ps4_vars.cpp)
else ()
    file(GLOB PPLAY_SCRAPPER_SRC src/scrapper/*.c*)
    list(APPEND PPLAY_SRC ${PPLAY_SCRAPPER_SRC})
endif ()

# includes
set(PPLAY_INC
        src
        src/catalog
        src/filer
        src/filer/Browser
        src/filer/ftplib
        src/menus
        src/player
        src/scrapper
        ${MPV_INCLUDE_DIRS}
        ${ASS_INCLUDE_DIRS}
        ${FFMPEG_INCLUDE_DIRS}
        external/cJSON
        )
# BZip2 is not used on the PS4 streaming playback path; the find_package
# call above is gated on NOT PLATFORM_PS4, so ${BZIP2_INCLUDE_DIR} is
# NOTFOUND here. Append it only when the variable was actually set.
if (NOT PLATFORM_PS4)
    list(APPEND PPLAY_INC ${BZIP2_INCLUDE_DIR})
endif ()

# cflags
set(PPLAY_CFLAGS
        -D__PPLAY__
        -D__VERSION_MAJOR__=${VERSION_MAJOR}
        -D__VERSION_MINOR__=${VERSION_MINOR}
        )
# __PS4__ is the source-level marker the pplay code uses to gate PS4-only
# behaviour (paths, stub classes, omitted UI). Define it whenever the
# PS4 build is selected; #ifdef __PS4__ blocks in the headers/sources
# pick it up automatically.
if (PLATFORM_PS4)
    list(APPEND PPLAY_CFLAGS -D__PS4__)
endif ()

# ldflags
set(PPLAY_LDFLAGS
        ${MPV_LIBRARY}
        ${ASS_LIBRARIES}
        ${FFMPEG_LIBRARIES}
        ${BZIP2_LIBRARIES}
        )

if (OPTION_SMB2)
    set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
    set(OPENSSL_INCLUDE_DIR "" CACHE STRING "" FORCE)
    add_subdirectory(libsmb2)
    list(APPEND PPLAY_CFLAGS -D__SMB2__)
    list(APPEND PPLAY_INC libsmb2/include libsmb2/include/smb2)
    list(APPEND PPLAY_LDFLAGS smb2)
endif ()

#####################
# PLATORM SPECIFIC
#####################
if (PLATFORM_SWITCH)
    #####################
    # SWITCH PLATORM
    #####################
    set(MPV_LIBRARIES mpv)
    list(APPEND PPLAY_INC)
    list(APPEND PPLAY_LDFLAGS
            swscale swresample avformat avfilter avcodec avutil
            lzma opus vpx ass freetype fribidi png bz2 usbhsfs ntfs-3g lwext4
            )
elseif (PLATFORM_PS4)
    set(PS4_PKG_TITLE "pplay @ cpasjuste")
    set(PS4_PKG_TITLE_ID "PPLA00001")
    set(PS4_PKG_VERSION "${VERSION_MAJOR}.${VERSION_MINOR}")
    list(APPEND PPLAY_INC)
    # The OpenOrbisSDK does not ship static libs for opus / fribidi /
    # png / bz2 (the Sony SDK only ships the Sce* .so runtime libs
    # for the dynamic PS4 firmware). For the PS4 streaming playback
    # path we rely on FFmpeg's built-in Opus / H.264 / AAC decoders +
    # SDL2's bundled png/jpeg loaders, so we don't need any of these
    # in the link line. zlib is pulled in transitively via
    # libavformat. freetype is part of libcross2d (linked in via
    # C2D_LDFLAGS), not pplay itself.
elseif (PLATFORM_LINUX)
    #####################
    # LINUX PLATORM
    #####################
endif ()

#####################
# pplay executable
#####################
add_executable(${CMAKE_PROJECT_NAME} ${PPLAY_SRC})
target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE ${PPLAY_INC})
target_compile_options(${CMAKE_PROJECT_NAME} PRIVATE ${PPLAY_CFLAGS})
target_compile_options(${CMAKE_PROJECT_NAME} PRIVATE $<$<COMPILE_LANGUAGE:CXX>:-fno-rtti>)
target_link_libraries(${CMAKE_PROJECT_NAME} cross2d ${PPLAY_LDFLAGS})
if (NOT PLATFORM_PS4)
    target_link_libraries(${CMAKE_PROJECT_NAME} pscrap)
endif ()

#####################
# tests (catalog)
#####################
enable_testing()
add_subdirectory(tests/catalog)

#####################
# targets
#####################
include(libcross2d/cmake/targets.cmake)
