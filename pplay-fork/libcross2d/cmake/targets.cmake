cmake_minimum_required(VERSION 3.16)
#set(CMAKE_VERBOSE_MAKEFILE ON)

###########################################################
# PS4 packaging macros
#
# Upstream cpasjuste/libcross2d expects these to be provided by the
# toolchain installer (OpenOrbis PS4 Toolchain.pkg) via the
# `${OPENORBIS}/cmake/OpenOrbisConfig.cmake` include. Neither the v0.5.2
# source nor the openorbisofficial/toolchain Docker image ship them
# (the official Docker image only contains the binaries under
# /usr/lib/OpenOrbisSDK/bin/linux). We therefore define the macros here
# so the cmake `add_self` / `add_pkg` calls in the PS4 block below
# always resolve regardless of toolchain origin. The wire-up is what
# the macOS installer does internally: invoke PkgTool.Core and
# create-fself, both of which the openorbisofficial image already
# provides via /opt/oo/bin/linux/.
#
# The official OpenOrbis image only ships `create-eboot` (Sony's
# tool with `-in/-out/-ptype/-paid` flags), not the upstream
# OpenOrbis/create-fself which uses `--eboot` + positional args.
# Going through create-eboot requires Go 1.17 to build upstream
# create-fself from source (Ubuntu 20.04 only ships Go 1.14), and
# upstream go modules use Windows-style backslashes in `replace`
# directives that break on Linux. So we use create-eboot directly
# via flag syntax (same binary, same output). ${CREATE_FSELF}
# defaults to `create-fself` (PATH-resolved by the docker image's
# /opt/oo/bin/linux layout) and can be overridden with
# -DCREATE_FSELF=/full/path if a custom binary is preferred.
#
# Output convention (matches Sony PS4 homebrew expectations):
#   <project>.elf            raw unstripped ELF64 from CMake
#   <project>.eboot.bin      fake-signed ELF (paid=0x3800000000000011,
#                            ptype=npdrm_exec) — GoldHEN accepts this.
#   sce_sys/param.sfo        PS4 system param.sfo
#   sce_sys/about/           right.sprx, icon0.png (from data/ps4/romfs)
#   <titleid>.pkg            final installable .pkg built by PkgTool.Core.
###########################################################
if (PLATFORM_PS4 AND NOT COMMAND add_self)
    # create-fself lives at /opt/oo/bin/linux/create-fself.bin (the
    # path the openorbisofficial/toolchain Docker image installs it
    # under; the OpenOrbis macOS .pkg drops it under
    # /usr/lib/OpenOrbisSDK/bin/linux/ and our Dockerfile.ps4 copies
    # from there).
    #
    # We do NOT invoke create-fself directly from cmake because the
    # Unix Makefiles generator escapes arg values with `\"...\"`
    # sequences that dash (Debian's /bin/sh) treats as literal
    # characters — create-fself then reports "no such file" because
    # it's looking for a path called `\"/work/build/pplay\"`. Use the
    # bash wrapper at scripts/ps4-toolchain/pplay-create-fself.sh
    # which reads input/output paths from positional args that cmake
    # leaves unescaped (verified locally with a no-VERBATIM cmake
    # probe).
    #
    # pplay-fork layout: libcross2d/cmake/targets.cmake and
    # scripts/ps4-toolchain/pplay-create-fself.sh live 3 dirs apart
    # (cmake → libcross2d → pplay-fork → scripts). Walk up two levels
    # then down into scripts/.
    set(PPLAY_FSELF_WRAPPER "${CMAKE_CURRENT_LIST_DIR}/../../scripts/ps4-toolchain/pplay-create-fself.sh")
    if (NOT EXISTS "${PPLAY_FSELF_WRAPPER}")
        # Fallback for callers that don't have scripts/ co-located
        # (e.g. when libcross2d is consumed as a git submodule from a
        # different repo). In that case just point at create-fself
        # directly and let the caller deal with the dash escape
        # quirk — they probably aren't on Debian.
        set(PPLAY_FSELF_WRAPPER "/opt/oo/bin/linux/create-fself")
    endif ()
    function(add_self PROJECT)
        set(SELF_BIN "${CMAKE_CURRENT_BINARY_DIR}/${PROJECT}.elf")
        add_custom_command(
                TARGET ${PROJECT} POST_BUILD
                COMMAND ${CMAKE_COMMAND} -E copy
                    "$<TARGET_FILE:${PROJECT}>"
                    "${SELF_BIN}"
                COMMENT "add_self: copying ${PROJECT} -> ${SELF_BIN}"
                VERBATIM
        )
        add_custom_target(${PROJECT}.eboot
                DEPENDS ${PROJECT}
                COMMAND ${CMAKE_COMMAND} -E make_directory
                    "${CMAKE_CURRENT_BINARY_DIR}/eboot"
                # create-eboot (Sony) crashes with "Failed to build FSELF:
                # EOF" when -out points to a path whose basename is
                # literally "eboot.bin" — empirically verified across
                # ptype=fake / npdrm_exec / system_exec. Workaround:
                # write to .fself then rename. Self-trust:
                # verified by hand in ps4-uk-build container.
                #
                # The bash wrapper (PPLAY_FSELF_WRAPPER) takes the
                # paths as positional args — cmake leaves those
                # unescaped, so the dash /bin/sh escape-quirk is
                # bypassed. See comment above for the full rationale.
                COMMAND ${PPLAY_FSELF_WRAPPER}
                    "$<TARGET_FILE:${PROJECT}>"
                    "${CMAKE_CURRENT_BINARY_DIR}/eboot/eboot.bin.fself"
                    npdrm_exec
                    0x3800000000000011
                COMMAND ${CMAKE_COMMAND} -E rename
                    "${CMAKE_CURRENT_BINARY_DIR}/eboot/eboot.bin.fself"
                    "${CMAKE_CURRENT_BINARY_DIR}/eboot/eboot.bin"
                COMMENT "add_self: create-fself -> eboot.bin (paid=0x3800000000000011, ptype=npdrm_exec)"
                VERBATIM
        )
    endfunction()

    function(add_pkg PROJECT ROMFS_DIR TITLE_ID TITLE VERSION)
        set(PKG_DIR "${CMAKE_CURRENT_BINARY_DIR}/${TITLE_ID}")
        set(PKG_OUT "${CMAKE_CURRENT_BINARY_DIR}/${TITLE_ID}.pkg")
        # sce_sys param.sfo comes from data/ps4/romfs/sce_sys (right.sprx,
        # icon0.png) bundled by libcross2d upsteam. We only need to make
        # sure the FS layout that PkgTool.Core expects is present.
        add_custom_target(${PROJECT}_pkg
                DEPENDS ${PROJECT}.eboot
                # Generate pkg.gp4 first so PkgTool.Core has something
                # to read. The generator script is also written below
                # (the file(WRITE) is at function scope so it always
                # runs at configure time; the PRE_BUILD hook then
                # runs `cmake -P` against it on each build).
                COMMAND ${CMAKE_COMMAND} -P "${CMAKE_CURRENT_BINARY_DIR}/gen_pkg_gp4.cmake"
                COMMAND ${CMAKE_COMMAND} -E remove_directory "${PKG_DIR}"
                COMMAND ${CMAKE_COMMAND} -E make_directory "${PKG_DIR}/sce_sys"
                COMMAND ${CMAKE_COMMAND} -E make_directory "${PKG_DIR}/eboot.bin"
                COMMAND ${CMAKE_COMMAND} -E copy_directory
                    "${ROMFS_DIR}/sce_sys" "${PKG_DIR}/sce_sys"
                COMMAND ${CMAKE_COMMAND} -E copy
                    "${CMAKE_CURRENT_BINARY_DIR}/eboot/eboot.bin"
                    "${PKG_DIR}/eboot.bin/eboot.bin"
                COMMAND PkgTool.Core
                    pkg_build
                    "${CMAKE_CURRENT_BINARY_DIR}/pkg.gp4"
                    "${CMAKE_CURRENT_BINARY_DIR}"
                COMMENT "add_pkg: building ${TITLE_ID}.pkg via PkgTool.Core"
                VERBATIM
        )
        # Generate pkg.gp4 on the fly. PkgTool.Core expects a pkg.gp4
        # project file alongside the eboot.bin / sce_sys layout that
        # we just materialised; without it the pkg build aborts with
        # "Could not find file 'pkg.gp4'".
        #
        # The pkg.gp4 schema (see opt/oo/samples/hello_world/pkg.gp4)
        # is the OpenOrbis XML project descriptor consumed by
        # PkgTool.Core's `pkg_build` subcommand. We write a
        # generator cmake script to gen_pkg_gp4.cmake and add a
        # custom command that runs `cmake -P` against it before
        # PkgTool.Core is invoked. See ps4-uk-stream plan
        # docs/superpowers/plans/2026-08-01-ps4-uk-stream-impl.md.
        set(GP4_PATH "${CMAKE_CURRENT_BINARY_DIR}/pkg.gp4")
        set(GEN_SCRIPT "${CMAKE_CURRENT_BINARY_DIR}/gen_pkg_gp4.cmake")
        file(WRITE "${GEN_SCRIPT}"
             "set(TITLE_ID \"${TITLE_ID}\")\n"
             "set(TITLE \"${TITLE}\")\n"
             "set(GP4_PATH \"${GP4_PATH}\")\n"
             "set(PKG_DIR \"${CMAKE_CURRENT_BINARY_DIR}/${TITLE_ID}\")\n"
             "file(WRITE \"\${GP4_PATH}\"\n"
             "  \"<?xml version=\\\"1.0\\\"?>\\n\"\n"
             "  \"<psproject xmlns:xsd=\\\"http://www.w3.org/2001/XMLSchema\\\"\"\n"
             "  \" xmlns:xsi=\\\"http://www.w3.org/2001/XMLSchema-instance\\\"\"\n"
             "  \" fmt=\\\"gp4\\\" version=\\\"1000\\\">\\n\"\n"
             "  \"  <volume>\\n\"\n"
             "  \"    <volume_type>pkg_ps4_app</volume_type>\\n\"\n"
             "  \"    <volume_id>PS4VOLUME</volume_id>\\n\"\n"
             "  \"    <volume_ts>2026-08-02 08:12:00</volume_ts>\\n\"\n"
             "  \"    <package content_id=\\\"IV0000-\${TITLE_ID}_00-PPLAY00000000000\\\"\"\n"
             "  \"             passcode=\\\"00000000000000000000000000000000\\\"\"\n"
             "  \"             storage_type=\\\"digital50\\\" app_type=\\\"full\\\" />\\n\"\n"
             "  \"    <chunk_info chunk_count=\\\"1\\\" scenario_count=\\\"1\\\">\\n\"\n"
             "  \"      <chunks>\\n\"\n"
             "  \"        <chunk id=\\\"0\\\" layer_no=\\\"0\\\" label=\\\"Chunk #0\\\" />\\n\"\n"
             "  \"      </chunks>\\n\"\n"
             "  \"      <scenarios default_id=\\\"0\\\">\\n\"\n"
             "  \"        <scenario id=\\\"0\\\" type=\\\"sp\\\"\"\n"
             "  \"                  initial_chunk_count=\\\"1\\\"\"\n"
             "  \"                  label=\\\"Scenario #0\\\">0</scenario>\\n\"\n"
             "  \"      </scenarios>\\n\"\n"
             "  \"    </chunk_info>\\n\"\n"
             "  \"  </volume>\\n\"\n"
             "  \"  <files img_no=\\\"0\\\">\\n\"\n"
             "  \"    <file targ_path=\\\"eboot.bin\\\" orig_path=\\\"\${PKG_DIR}/eboot.bin/eboot.bin\\\" />\\n\"\n"
             "  \"    <file targ_path=\\\"sce_sys/about/right.sprx\\\" orig_path=\\\"\${PKG_DIR}/sce_sys/about/right.sprx\\\" />\\n\"\n"
             "  \"    <file targ_path=\\\"sce_sys/param.sfo\\\" orig_path=\\\"\${PKG_DIR}/sce_sys/param.sfo\\\" />\\n\"\n"
             "  \"    <file targ_path=\\\"sce_sys/icon0.png\\\" orig_path=\\\"\${PKG_DIR}/sce_sys/icon0.png\\\" />\\n\"\n"
             "  \"  </files>\\n\"\n"
             "  \"  <rootdir>\\n\"\n"
             "  \"    <dir targ_name=\\\"sce_sys\\\">\\n\"\n"
             "  \"      <dir targ_name=\\\"about\\\" />\\n\"\n"
             "  \"    </dir>\\n\"\n"
             "  \"  </rootdir>\\n\"\n"
             "  \"</psproject>\\n\")\n")
    endfunction()
endif ()

###########################################################
# Copy data to binary directory (common to all platforms)
###########################################################
add_custom_target(${PROJECT_NAME}.data
        # cleanup data in binary dir
        COMMAND ${CMAKE_COMMAND} -E remove_directory ${CMAKE_CURRENT_BINARY_DIR}/data_romfs
        COMMAND ${CMAKE_COMMAND} -E remove_directory ${CMAKE_CURRENT_BINARY_DIR}/data_datadir
        COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_CURRENT_BINARY_DIR}/data_romfs
        COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_CURRENT_BINARY_DIR}/data_datadir
        # copy data to binary directory, for program execution when invoked from cmake build directory
        COMMAND ${CMAKE_COMMAND} -D SRC=${CMAKE_CURRENT_SOURCE_DIR}/data/common/datadir -D DST=${CMAKE_CURRENT_BINARY_DIR} -P ${CMAKE_CURRENT_LIST_DIR}/copy_directory_custom.cmake
        COMMAND ${CMAKE_COMMAND} -D SRC=${CMAKE_CURRENT_SOURCE_DIR}/data/${TARGET_PLATFORM}/datadir -D DST=${CMAKE_CURRENT_BINARY_DIR} -P ${CMAKE_CURRENT_LIST_DIR}/copy_directory_custom.cmake
        # cache data to binary directory
        # this allow parent projects to add files here before packaging (make project-name_target_release)
        COMMAND ${CMAKE_COMMAND} -D SRC=${CMAKE_CURRENT_SOURCE_DIR}/data/common/romfs -D DST=${CMAKE_CURRENT_BINARY_DIR}/data_romfs -P ${CMAKE_CURRENT_LIST_DIR}/copy_directory_custom.cmake
        COMMAND ${CMAKE_COMMAND} -D SRC=${CMAKE_CURRENT_SOURCE_DIR}/data/common/datadir -D DST=${CMAKE_CURRENT_BINARY_DIR}/data_datadir -P ${CMAKE_CURRENT_LIST_DIR}/copy_directory_custom.cmake
        COMMAND ${CMAKE_COMMAND} -D SRC=${CMAKE_CURRENT_SOURCE_DIR}/data/${TARGET_PLATFORM}/romfs -D DST=${CMAKE_CURRENT_BINARY_DIR}/data_romfs -P ${CMAKE_CURRENT_LIST_DIR}/copy_directory_custom.cmake
        COMMAND ${CMAKE_COMMAND} -D SRC=${CMAKE_CURRENT_SOURCE_DIR}/data/${TARGET_PLATFORM}/datadir -D DST=${CMAKE_CURRENT_BINARY_DIR}/data_datadir -P ${CMAKE_CURRENT_LIST_DIR}/copy_directory_custom.cmake
        )
add_dependencies(${PROJECT_NAME} ${PROJECT_NAME}.data)

########################
# Linux/Win64 targets
########################
if (PLATFORM_LINUX OR PLATFORM_WINDOWS)
    # romfs
    include(${cross2d_SOURCE_DIR}/cmake/romfs.cmake)
    add_romfs(${PROJECT_NAME} ${CMAKE_CURRENT_BINARY_DIR}/data_romfs)
    add_dependencies(${PROJECT_NAME}-romfs ${PROJECT_NAME}.data)
    # release
    set_target_properties(${PROJECT_NAME} PROPERTIES LINK_FLAGS_RELEASE -s)
    add_custom_target(${PROJECT_NAME}_${TARGET_PLATFORM}_release
            DEPENDS ${PROJECT_NAME}
            COMMAND ${CMAKE_COMMAND} -E remove -f ${CMAKE_BINARY_DIR}/${PROJECT_NAME}-${VERSION_MAJOR}.${VERSION_MINOR}_${TARGET_PLATFORM}.zip
            COMMAND ${CMAKE_COMMAND} -E remove_directory ${CMAKE_BINARY_DIR}/release/${PROJECT_NAME}
            COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_BINARY_DIR}/release/${PROJECT_NAME}
            COMMAND ${CMAKE_COMMAND} -E copy ${CMAKE_CURRENT_BINARY_DIR}/${PROJECT_NAME}${CMAKE_EXECUTABLE_SUFFIX} ${CMAKE_BINARY_DIR}/release/${PROJECT_NAME}/
            COMMAND ${CMAKE_COMMAND} -D SRC=${CMAKE_CURRENT_BINARY_DIR}/data_datadir -D DST=${CMAKE_BINARY_DIR}/release/${PROJECT_NAME} -P ${CMAKE_CURRENT_LIST_DIR}/copy_directory_custom.cmake
            COMMAND cd ${CMAKE_BINARY_DIR}/release && ${ZIP} -r ../${PROJECT_NAME}-${VERSION_MAJOR}.${VERSION_MINOR}_${TARGET_PLATFORM}.zip ${PROJECT_NAME}
            )
endif ()

###########################
# Nintendo Switch target
###########################
if (PLATFORM_SWITCH)
    set_target_properties(${PROJECT_NAME} PROPERTIES LINK_FLAGS_RELEASE -s)
    add_custom_target(${PROJECT_NAME}.nro
            DEPENDS ${PROJECT_NAME}
            DEPENDS ${PROJECT_NAME}.data
            COMMAND ${DEVKITPRO}/tools/bin/nacptool --create "${PROJECT_NAME}" "${PROJECT_AUTHOR}" "${VERSION_MAJOR}.${VERSION_MINOR}" ${PROJECT_NAME}.nacp
            # copy custom switch "romfs" data to common "romfs" data for romfs creation (merge/overwrite common data)
            COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_CURRENT_BINARY_DIR}/data_romfs
            COMMAND ${DEVKITPRO}/tools/bin/elf2nro ${PROJECT_NAME} ${PROJECT_NAME}.nro --icon=${CMAKE_CURRENT_SOURCE_DIR}/data/${TARGET_PLATFORM}/icon.jpg --nacp=${PROJECT_NAME}.nacp --romfsdir=${CMAKE_CURRENT_BINARY_DIR}/data_romfs)
    add_custom_target(${PROJECT_NAME}_${TARGET_PLATFORM}_release
            DEPENDS ${PROJECT_NAME}.nro
            COMMAND ${CMAKE_COMMAND} -E remove -f ${CMAKE_BINARY_DIR}/${PROJECT_NAME}-${VERSION_MAJOR}.${VERSION_MINOR}_${TARGET_PLATFORM}.zip
            COMMAND ${CMAKE_COMMAND} -E remove_directory ${CMAKE_BINARY_DIR}/release/${PROJECT_NAME}
            COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_BINARY_DIR}/release/${PROJECT_NAME}
            COMMAND ${CMAKE_COMMAND} -E copy ${CMAKE_CURRENT_BINARY_DIR}/${PROJECT_NAME}.nro ${CMAKE_BINARY_DIR}/release/${PROJECT_NAME}/
            COMMAND ${CMAKE_COMMAND} -D SRC=${CMAKE_CURRENT_BINARY_DIR}/data_datadir -D DST=${CMAKE_BINARY_DIR}/release/${PROJECT_NAME} -P ${CMAKE_CURRENT_LIST_DIR}/copy_directory_custom.cmake
            COMMAND cd ${CMAKE_BINARY_DIR}/release && ${ZIP} -r ../${PROJECT_NAME}-${VERSION_MAJOR}.${VERSION_MINOR}_${TARGET_PLATFORM}.zip ${PROJECT_NAME}
            )
endif (PLATFORM_SWITCH)

#####################
# VITA target
#####################
if (PLATFORM_VITA)
    add_custom_target(${PROJECT_NAME}.vpk
            DEPENDS ${PROJECT_NAME}
            DEPENDS ${PROJECT_NAME}.data
            # create eboot
            COMMAND ${VITASDK}/bin/vita-elf-create ${PROJECT_NAME} ${CMAKE_CURRENT_BINARY_DIR}/${PROJECT_NAME}.velf
            COMMAND ${VITASDK}/bin/vita-make-fself -c ${PROJECT_NAME}.velf eboot.bin
            # create vpk
            COMMAND ${CMAKE_COMMAND} -E remove_directory ${CMAKE_CURRENT_BINARY_DIR}/vpk
            COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_CURRENT_BINARY_DIR}/vpk
            COMMAND ${CMAKE_COMMAND} -D SRC=${CMAKE_CURRENT_BINARY_DIR}/data_romfs -D DST=${CMAKE_CURRENT_BINARY_DIR}/vpk -P ${CMAKE_CURRENT_LIST_DIR}/copy_directory_custom.cmake
            COMMAND ${CMAKE_COMMAND} -E copy eboot.bin ${CMAKE_CURRENT_BINARY_DIR}/vpk/eboot.bin
            COMMAND ${VITASDK}/bin/vita-mksfoex -s TITLE_ID="${TITLE_ID}" "${PROJECT_NAME}" ${CMAKE_CURRENT_BINARY_DIR}/vpk/sce_sys/param.sfo
            COMMAND cd ${CMAKE_CURRENT_BINARY_DIR}/vpk && ${ZIP} -r ../${PROJECT_NAME}.vpk .
            )
    add_custom_target(${PROJECT_NAME}_${TARGET_PLATFORM}_release
            DEPENDS ${PROJECT_NAME}.vpk
            COMMAND ${CMAKE_COMMAND} -E remove -f ${CMAKE_BINARY_DIR}/${PROJECT_NAME}-${VERSION_MAJOR}.${VERSION_MINOR}_${TARGET_PLATFORM}.zip
            COMMAND ${CMAKE_COMMAND} -E remove_directory ${CMAKE_BINARY_DIR}/release/${PROJECT_NAME}
            COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_BINARY_DIR}/release/${PROJECT_NAME}
            COMMAND ${CMAKE_COMMAND} -E copy ${CMAKE_CURRENT_BINARY_DIR}/${PROJECT_NAME}.vpk ${CMAKE_BINARY_DIR}/release/${PROJECT_NAME}/
            COMMAND ${CMAKE_COMMAND} -D SRC=${CMAKE_CURRENT_BINARY_DIR}/data_datadir -D DST=${CMAKE_BINARY_DIR}/release/${PROJECT_NAME} -P ${CMAKE_CURRENT_LIST_DIR}/copy_directory_custom.cmake
            COMMAND cd ${CMAKE_BINARY_DIR}/release && ${ZIP} -r ../${PROJECT_NAME}-${VERSION_MAJOR}.${VERSION_MINOR}_${TARGET_PLATFORM}.zip ${PROJECT_NAME}
            )
endif (PLATFORM_VITA)

###########################
# PS4 target
###########################
if (PLATFORM_PS4)
    string(REPLACE "." "" PS4_PKG_VERSION_CLEAN ${PS4_PKG_VERSION})
    add_self(${PROJECT_NAME})
    add_pkg(${PROJECT_NAME} ${CMAKE_CURRENT_BINARY_DIR}/data_romfs ${PS4_PKG_TITLE_ID} ${PS4_PKG_TITLE} ${PS4_PKG_VERSION})
    add_dependencies(${PROJECT_NAME}_pkg ${PROJECT_NAME}.data)
    add_custom_target(${PROJECT_NAME}_${TARGET_PLATFORM}_release
            DEPENDS ${PROJECT_NAME}_pkg
            COMMAND ${CMAKE_COMMAND} -E remove -f ${CMAKE_BINARY_DIR}/${PROJECT_NAME}-${VERSION_MAJOR}.${VERSION_MINOR}_${TARGET_PLATFORM}.zip
            COMMAND ${CMAKE_COMMAND} -E remove_directory ${CMAKE_BINARY_DIR}/release/${PROJECT_NAME}
            COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_BINARY_DIR}/release/${PROJECT_NAME}
            COMMAND ${CMAKE_COMMAND} -E copy ${CMAKE_BINARY_DIR}/${PKG_OUT_NAME} ${CMAKE_BINARY_DIR}/release/${PROJECT_NAME}/
            COMMAND ${CMAKE_COMMAND} -D SRC=${CMAKE_CURRENT_BINARY_DIR}/data_datadir -D DST=${CMAKE_BINARY_DIR}/release/${PROJECT_NAME}/data/${PROJECT_NAME} -P ${CMAKE_CURRENT_LIST_DIR}/copy_directory_custom.cmake
            COMMAND cd ${CMAKE_BINARY_DIR}/release && ${ZIP} -r ../${PROJECT_NAME}-${VERSION_MAJOR}.${VERSION_MINOR}_${TARGET_PLATFORM}.zip ${PROJECT_NAME}
            )
endif (PLATFORM_PS4)

###########################
# Sony ps3 target
###########################
if (PLATFORM_PS3)
    set_target_properties(${PROJECT_NAME} PROPERTIES LINK_FLAGS_RELEASE -s)
    add_custom_target(${PROJECT_NAME}.self
            DEPENDS ${PROJECT_NAME}
            DEPENDS ${PROJECT_NAME}.data
            COMMAND ${CMAKE_COMMAND} -E copy ${PROJECT_NAME} ${PROJECT_NAME}.sprx
            COMMAND ${PSL1GHT}/bin/sprxlinker ${PROJECT_NAME}.sprx
            COMMAND ${PSL1GHT}/bin/make_self ${PROJECT_NAME}.sprx ${PROJECT_NAME}.self
            )
endif (PLATFORM_PS3)

###########################
# Dreamcast target
###########################
if (PLATFORM_DREAMCAST)
    set_target_properties(${PROJECT_NAME} PROPERTIES LINK_FLAGS_RELEASE -s)
    # romdisk handling (tricky..)
    add_custom_target(
            ${PROJECT_NAME}.romdisk ALL
            DEPENDS dummy_romdisk
    )
    add_custom_command(OUTPUT
            dummy_romdisk ${CMAKE_BINARY_DIR}/romdisk.o
            COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_CURRENT_BINARY_DIR}/data_romfs
            COMMAND ${KOS_BASE}/utils/genromfs/genromfs -f ${CMAKE_BINARY_DIR}/romdisk.img -d ${CMAKE_CURRENT_BINARY_DIR}/data_romfs -v
            COMMAND KOS_ARCH=${KOS_ARCH} KOS_AS=${KOS_AS} KOS_AFLAGS=${KOS_AFLAGS} KOS_LD=${KOS_LD} KOS_OBJCOPY=${KOS_OBJCOPY}
            /bin/bash ${KOS_BASE}/utils/bin2o/bin2o ${CMAKE_BINARY_DIR}/romdisk.img romdisk ${CMAKE_BINARY_DIR}/romdisk.o
            )
    target_sources(${PROJECT_NAME} PRIVATE ${CMAKE_BINARY_DIR}/romdisk.o)
    add_custom_target(${PROJECT_NAME}.bin
            DEPENDS ${PROJECT_NAME}
            DEPENDS ${PROJECT_NAME}.data
            COMMAND ${CMAKE_OBJCOPY} -R .stack -O binary ${PROJECT_NAME}.elf ${PROJECT_NAME}.bin
            )
    add_custom_target(${PROJECT_NAME}.cdi
            DEPENDS ${PROJECT_NAME}.bin
            COMMAND scramble ${PROJECT_NAME}.bin 1DS_CORE.BIN
            COMMAND genisoimage -V ${PROJECT_NAME} -G ${CMAKE_SOURCE_DIR}/data/dreamcast/IP.BIN -joliet -rock -l -x .svn -o ${PROJECT_NAME}.iso 1DS_CORE.BIN ${CMAKE_CURRENT_BINARY_DIR}/data_datadir
            COMMAND cdi4dc ${PROJECT_NAME}.iso ${PROJECT_NAME}.cdi -d >> cdi4dc.log
            )
endif (PLATFORM_DREAMCAST)

#####################
# 3DS target
#####################
# TODO: update target and packaging, see linux/windows/switch
if (PLATFORM_3DS)
    set_target_properties(${PROJECT_NAME} PROPERTIES LINK_FLAGS "-L${DEVKITPRO}/portlibs/3ds/lib -L${DEVKITPRO}/libctru/lib -specs=${DEVKITPRO}/devkitARM/arm-none-eabi/lib/3dsx.specs")
    add_custom_target(${PROJECT_NAME}.3dsx
            DEPENDS ${PROJECT_NAME}
            DEPENDS ${PROJECT_NAME}.data
            COMMAND ${DEVKITPRO}/tools/bin/smdhtool --create "${PROJECT_NAME}" "${PROJECT_NAME}" "${PROJECT_AUTHOR}" ${CMAKE_CURRENT_SOURCE_DIR}/data/${TARGET_PLATFORM}/icon.png ${PROJECT_NAME}.smdh
            COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_CURRENT_BINARY_DIR}/data_romfs
            COMMAND ${DEVKITPRO}/tools/bin/3dsxtool ${PROJECT_NAME} ${PROJECT_NAME}.3dsx --smdh=${PROJECT_NAME}.smdh --romfs=${CMAKE_CURRENT_BINARY_DIR}/data_romfs)
    add_custom_target(${PROJECT_NAME}_${TARGET_PLATFORM}_release
            DEPENDS ${PROJECT_NAME}.3dsx
            COMMAND ${CMAKE_COMMAND} -E remove -f ${CMAKE_BINARY_DIR}/${PROJECT_NAME}-${VERSION_MAJOR}.${VERSION_MINOR}_${TARGET_PLATFORM}.zip
            COMMAND ${CMAKE_COMMAND} -E remove_directory ${CMAKE_BINARY_DIR}/release/${PROJECT_NAME}
            COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_BINARY_DIR}/release/${PROJECT_NAME}
            COMMAND ${CMAKE_COMMAND} -E copy ${CMAKE_CURRENT_BINARY_DIR}/${PROJECT_NAME}.3dsx ${CMAKE_BINARY_DIR}/release/${PROJECT_NAME}/
            COMMAND ${CMAKE_COMMAND} -D SRC=${CMAKE_CURRENT_BINARY_DIR}/data_datadir -D DST=${CMAKE_BINARY_DIR}/release/${PROJECT_NAME} -P ${CMAKE_CURRENT_LIST_DIR}/copy_directory_custom.cmake
            COMMAND cd ${CMAKE_BINARY_DIR}/release && ${ZIP} -r ../${PROJECT_NAME}-${VERSION_MAJOR}.${VERSION_MINOR}_${TARGET_PLATFORM}.zip ${PROJECT_NAME}
            )
endif (PLATFORM_3DS)
