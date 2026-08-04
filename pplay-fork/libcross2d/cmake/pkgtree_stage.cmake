# pkgtree_stage.cmake — stage the PS4 runtime tree into PPLA00001/.
#
# Invoked from pplay-fork/libcross2d/cmake/targets.cmake's
# `add_pkgtree(PROJECT)` via `cmake -P`, with the standard cmake
# variables set via -D. The script is intentionally runnable standalone
# (no project() call) so it can be exercised by tests without needing
# the OpenOrbis SDK or a full PLATFORM_PS4 cmake configure.
#
# Stages:
#   ${ROMFS_DIR}/<subdir>     -> ${PKG_DIR}/<subdir>     (sce_sys, sce_module, skin, ...)
#   ${DATADIR_DIR}/<subdir>   -> ${PKG_DIR}/<subdir>     (mpv, ...)
#   ${EBOOT_BIN}              -> ${PKG_DIR}/eboot.bin/eboot.bin   (PkgTool.Core nested path)
#
# Idempotent: the entire ${PKG_DIR} is wiped before staging so re-running
# produces a byte-identical tree (assuming the sources did not change).

cmake_minimum_required(VERSION 3.16)

# --- input validation ----------------------------------------------------

if (NOT DEFINED PKG_DIR OR PKG_DIR STREQUAL "")
    message(FATAL_ERROR "pkgtree_stage.cmake: PKG_DIR is required (e.g. -DPKG_DIR=/work/build/PPLA00001)")
endif ()
if (NOT DEFINED ROMFS_DIR)
    message(FATAL_ERROR "pkgtree_stage.cmake: ROMFS_DIR is required")
endif ()
if (NOT DEFINED DATADIR_DIR)
    message(FATAL_ERROR "pkgtree_stage.cmake: DATADIR_DIR is required")
endif ()
if (NOT DEFINED EBOOT_BIN OR EBOOT_BIN STREQUAL "")
    message(FATAL_ERROR "pkgtree_stage.cmake: EBOOT_BIN is required")
endif ()

# --- helpers -------------------------------------------------------------

# Copy ${SRC_DIR} into ${PKG_DIR}/${subname} if ${SRC_DIR} exists and is a
# directory. ${SRC_DIR} is allowed to be missing — in that case we skip
# the copy silently and do not create an empty destination dir (matches
# copy_directory_custom.cmake's permissive semantics).
function(copy_if_present SRC_DIR PKG_DIR SUB)
    if (NOT EXISTS "${SRC_DIR}")
        return()
    endif ()
    execute_process(
        COMMAND "${CMAKE_COMMAND}" -E make_directory "${PKG_DIR}/${SUB}"
        RESULT_VARIABLE _mkdir_rc
    )
    if (NOT _mkdir_rc EQUAL 0)
        message(FATAL_ERROR "pkgtree_stage.cmake: mkdir ${PKG_DIR}/${SUB} failed (rc=${_mkdir_rc})")
    endif ()
    execute_process(
        COMMAND "${CMAKE_COMMAND}" -E copy_directory "${SRC_DIR}" "${PKG_DIR}/${SUB}"
        RESULT_VARIABLE _rc
    )
    if (NOT _rc EQUAL 0)
        message(FATAL_ERROR "pkgtree_stage.cmake: copy_directory ${SRC_DIR} -> ${PKG_DIR}/${SUB} failed (rc=${_rc})")
    endif ()
endfunction()

# Stage one source tree (ROMFS_DIR or DATADIR_DIR). For each immediate
# subdirectory of ${SRC_DIR}, copy it wholesale into ${PKG_DIR}/<subname>.
# ${SRC_DIR} may be absent.
function(stage_tree SRC_DIR PKG_DIR)
    if (NOT EXISTS "${SRC_DIR}")
        return()
    endif ()
    file(GLOB _children LIST_DIRECTORIES TRUE "${SRC_DIR}/*")
    foreach (_child IN LISTS _children)
        if (IS_DIRECTORY "${_child}")
            get_filename_component(_sub "${_child}" NAME)
            copy_if_present("${_child}" "${PKG_DIR}" "${_sub}")
        endif ()
    endforeach ()
    set(_children)
endfunction()

# --- main ----------------------------------------------------------------

# 1. wipe + recreate the destination — guarantees idempotency.
execute_process(
    COMMAND "${CMAKE_COMMAND}" -E remove_directory "${PKG_DIR}"
    RESULT_VARIABLE _rm_rc
)
if (NOT _rm_rc EQUAL 0 AND NOT _rm_rc EQUAL 1)
    message(FATAL_ERROR "pkgtree_stage.cmake: remove_directory ${PKG_DIR} failed (rc=${_rm_rc})")
endif ()
execute_process(
    COMMAND "${CMAKE_COMMAND}" -E make_directory "${PKG_DIR}"
    RESULT_VARIABLE _mkdir_rc
)
if (NOT _mkdir_rc EQUAL 0)
    message(FATAL_ERROR "pkgtree_stage.cmake: mkdir ${PKG_DIR} failed (rc=${_mkdir_rc})")
endif ()

# 2. stage romfs and datadir subtrees (each may be absent).
stage_tree("${ROMFS_DIR}" "${PKG_DIR}")
stage_tree("${DATADIR_DIR}" "${PKG_DIR}")

# 3. eboot.bin -> ${PKG_DIR}/eboot.bin/eboot.bin (PkgTool.Core convention).
execute_process(
    COMMAND "${CMAKE_COMMAND}" -E make_directory "${PKG_DIR}/eboot.bin"
    RESULT_VARIABLE _mkdir_rc
)
if (NOT _mkdir_rc EQUAL 0)
    message(FATAL_ERROR "pkgtree_stage.cmake: mkdir ${PKG_DIR}/eboot.bin failed (rc=${_mkdir_rc})")
endif ()
execute_process(
    COMMAND "${CMAKE_COMMAND}" -E copy "${EBOOT_BIN}" "${PKG_DIR}/eboot.bin/eboot.bin"
    RESULT_VARIABLE _rc
)
if (NOT _rc EQUAL 0)
    message(FATAL_ERROR "pkgtree_stage.cmake: copy ${EBOOT_BIN} -> ${PKG_DIR}/eboot.bin/eboot.bin failed (rc=${_rc})")
endif ()

# 4. Sanity banner for the runbook / pytest harness.
file(GLOB _all_files LIST_DIRECTORIES TRUE "${PKG_DIR}/*")
foreach (_entry IN LISTS _all_files)
    if (IS_DIRECTORY "${_entry}")
        file(GLOB_RECURSE _staged "${_entry}/*")
        list(LENGTH _staged _n)
        message(STATUS "PKGTREE: ${_entry}: ${_n} files")
        set(_staged)
    endif ()
endforeach ()
message(STATUS "PKGTREE: staged tree at ${PKG_DIR}")
