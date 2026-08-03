# This CMakeLists is intended for the FULL pPlay build (which provides cross2d).
# The standalone harness for sandbox-friendly testing lives in tests/standalone-catalog/.
add_executable(test_json test_json.cpp
    ../../src/catalog/Json.cpp
    ../../external/cJSON/cJSON.c
)
target_include_directories(test_json PRIVATE
    ../../src/catalog
    ../../external/cJSON
    ${CMAKE_CURRENT_SOURCE_DIR}/../../external/cJSON
)
# target_link_libraries(test_json cross2d)  # full build only

add_executable(test_catalog_api test_catalog_api.cpp
    ../../src/catalog/Json.cpp
    ../../src/catalog/CatalogApi.cpp
    # CatalogApi instantiates cs::DiskPosterCache in setPosterCacheDir; the
    # full-build linker needs its translation unit alongside CatalogApi.cpp
    # to resolve the symbols. The standalone harness
    # (tests/standalone-catalog/CMakeLists.txt's
    # c2d_add_catalog_test_with_api) bundles it; the full-build target
    # was missed when test_catalog_api was first added.
    ../../src/catalog/PosterCache.cpp
    ../../external/cJSON/cJSON.c
)
target_include_directories(test_catalog_api PRIVATE
    ../../src/catalog
    ../../external/cJSON
    ${CMAKE_CURRENT_SOURCE_DIR}/../../external/cJSON
)

add_executable(test_keyboard test_keyboard.cpp
    ../../src/catalog/OnscreenKeyboard.cpp
)
target_include_directories(test_keyboard PRIVATE
    ../../src/catalog
)
