# gpkg_gen.cmake — generate build/pkg.gp4 by enumerating ${PKG_DIR}.
#
# Invoked from pplay-fork/libcross2d/cmake/targets.cmake's
# `add_pkg(PROJECT ROMFS_DIR TITLE_ID TITLE VERSION)` via `cmake -P`,
# with the standard cmake variables set via -D. The script is
# intentionally runnable standalone (no project() call) so it can be
# exercised by tests without needing the OpenOrbis SDK or a full
# PLATFORM_PS4 cmake configure.
#
# Replaces the hardcoded 4-file `<files>` block that shipped with the
# upstream pPlay fork. See issue #96: the staged tree under
# ${PKG_DIR} (produced by `pplay.pkgtree`) is now the source of
# truth — every file in that tree is enumerated into the gp4 so
# PkgTool.Core packages them all into the .CNT.
#
# Output format reference: opt/oo/samples/hello_world/pkg.gp4.
# Required: targ_path (path inside the PKG payload) and orig_path
# (host filesystem path). eboot.bin/eboot.bin is the nested-path
# convention PkgTool.Core requires when the file lives inside a
# directory of the same name.

cmake_minimum_required(VERSION 3.16)

# --- input validation ----------------------------------------------------

if (NOT DEFINED PKG_DIR OR PKG_DIR STREQUAL "")
    message(FATAL_ERROR "gpkg_gen.cmake: PKG_DIR is required (e.g. -DPKG_DIR=/work/build/PPLA00001)")
endif ()
if (NOT DEFINED GP4_PATH OR GP4_PATH STREQUAL "")
    message(FATAL_ERROR "gpkg_gen.cmake: GP4_PATH is required")
endif ()
if (NOT DEFINED TITLE_ID OR TITLE_ID STREQUAL "")
    message(FATAL_ERROR "gpkg_gen.cmake: TITLE_ID is required")
endif ()
if (NOT DEFINED TITLE)
    # TITLE is the only optional var — default to TITLE_ID.
    set(TITLE "${TITLE_ID}")
endif ()
if (NOT DEFINED CONTENT_ID)
    # Default content_id follows the Sony convention
    # "IV0000-<TITLE_ID>_00-<16-hex-content-id>". The trailing 16-hex
    # is a build-specific value (PPLAY00000000000 for us) — overridable
    # via -DCONTENT_ID=… for content-id-specific builds.
    set(CONTENT_ID "IV0000-${TITLE_ID}_00-PPLAY00000000000")
endif ()
if (NOT DEFINED PASSCODE)
    # PkgTool.Core requires a 32-hex-digit passcode. All-zeros is the
    # OpenOrbis sample default; we keep it but allow override for
    # content-protected builds.
    set(PASSCODE "00000000000000000000000000000000")
endif ()
if (NOT DEFINED VOLUME_TS)
    # Default to the current UTC time. Format must be "YYYY-MM-DD
    # HH:MM:SS" per the OpenOrbis schema. Falling back to a fixed
    # date would defeat reproducibility hygiene — derive from
    # `date -u` instead.
    execute_process(
        COMMAND date -u "+%Y-%m-%d %H:%M:%S"
        OUTPUT_VARIABLE VOLUME_TS
        OUTPUT_STRIP_TRAILING_WHITESPACE
    )
endif ()
if (NOT EXISTS "${PKG_DIR}")
    message(FATAL_ERROR "gpkg_gen.cmake: PKG_DIR does not exist: ${PKG_DIR}")
endif ()

# --- enumerate the staged tree ------------------------------------------

# file(GLOB_RECURSE ... RELATIVE ...) gives us paths like
# "sce_module/libc.prx" or "eboot.bin/eboot.bin" — exactly what
# targ_path needs to be.
file(GLOB_RECURSE _staged_files RELATIVE "${PKG_DIR}" "${PKG_DIR}/*")
list(SORT _staged_files)

# --- build <rootdir> ----------------------------------------------------

# Top-level dirs: take the first path segment of every staged file
# and dedupe. e.g. "sce_sys/about/right.sprx" → top-level "sce_sys".
# "eboot.bin/eboot.bin" → top-level "eboot.bin".
set(_top_dirs "")
foreach (_f IN LISTS _staged_files)
    string(REGEX MATCH "^([^/]+)/" _m "${_f}")
    if (NOT _m STREQUAL "")
        list(APPEND _top_dirs "${CMAKE_MATCH_1}")
    endif ()
endforeach ()
list(REMOVE_DUPLICATES _top_dirs)
list(SORT _top_dirs)

# Depth-2 dirs: same idea but split on "/", take segments[1]. e.g.
# "sce_sys/about/right.sprx" → nested "about" under "sce_sys".
# We collect these inline in the <rootdir> emission below (no need
# for a separate pass).

# --- emit the XML -------------------------------------------------------

# We build the body as a list of lines, then file(WRITE) at the end.
# list(JOIN) + file(WRITE) is simpler than concatenating strings.
set(_lines "")

# Header + volume envelope (these are static — they describe the
# package metadata, not the file payload).
list(APPEND _lines "<?xml version=\"1.0\"?>")
list(APPEND _lines "<psproject xmlns:xsd=\"http://www.w3.org/2001/XMLSchema\" xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" fmt=\"gp4\" version=\"1000\">")
list(APPEND _lines "  <volume>")
list(APPEND _lines "    <volume_type>pkg_ps4_app</volume_type>")
list(APPEND _lines "    <volume_id>PS4VOLUME</volume_id>")
list(APPEND _lines "    <volume_ts>${VOLUME_TS}</volume_ts>")
list(APPEND _lines "    <package content_id=\"${CONTENT_ID}\"             passcode=\"${PASSCODE}\"             storage_type=\"digital50\" app_type=\"full\" />")
list(APPEND _lines "    <chunk_info chunk_count=\"1\" scenario_count=\"1\">")
list(APPEND _lines "      <chunks>")
list(APPEND _lines "        <chunk id=\"0\" layer_no=\"0\" label=\"Chunk #0\" />")
list(APPEND _lines "      </chunks>")
list(APPEND _lines "      <scenarios default_id=\"0\">")
list(APPEND _lines "        <scenario id=\"0\" type=\"sp\"                  initial_chunk_count=\"1\"                  label=\"Scenario #0\">0</scenario>")
list(APPEND _lines "      </scenarios>")
list(APPEND _lines "    </chunk_info>")
list(APPEND _lines "  </volume>")

# <files> — one <file> per staged file. targ_path is relative; orig_path
# is the host path. PkgTool.Core reads the orig_path as-is.
list(APPEND _lines "  <files img_no=\"0\">")
foreach (_f IN LISTS _staged_files)
    list(APPEND _lines "    <file targ_path=\"${_f}\" orig_path=\"${PKG_DIR}/${_f}\" />")
endforeach ()
list(APPEND _lines "  </files>")

# <rootdir> — one <dir> per top-level subdir, with nested <dir> for
# any depth-2 subdirs (e.g. sce_sys/about). PkgTool.Core accepts
# files anywhere; this block is mostly informational.
list(APPEND _lines "  <rootdir>")
foreach (_top IN LISTS _top_dirs)
    list(APPEND _lines "    <dir targ_name=\"${_top}\">")
    # Collect the nested children for this top-level dir.
    set(_children "")
    foreach (_f IN LISTS _staged_files)
        if (_f MATCHES "^${_top}/([^/]+)/")
            list(APPEND _children "${CMAKE_MATCH_1}")
        endif ()
    endforeach ()
    list(REMOVE_DUPLICATES _children)
    list(SORT _children)
    foreach (_child IN LISTS _children)
        list(APPEND _lines "      <dir targ_name=\"${_child}\" />")
    endforeach ()
    list(APPEND _lines "    </dir>")
endforeach ()
list(APPEND _lines "  </rootdir>")
list(APPEND _lines "</psproject>")

string(JOIN "\n" _body ${_lines})
# file(WRITE ...) doesn't append a trailing newline; pkg.gp4 examples
# have one, so add it explicitly.
file(WRITE "${GP4_PATH}" "${_body}\n")

# Sanity banner — mirrors the PKGTREE: banner for the runbook / pytest harness.
list(LENGTH _staged_files _n)
message(STATUS "GPKG: enumerated ${_n} files from ${PKG_DIR} into ${GP4_PATH}")
