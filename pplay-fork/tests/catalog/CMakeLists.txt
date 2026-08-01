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
    ../../external/cJSON/cJSON.c
)
target_include_directories(test_catalog_api PRIVATE
    ../../src/catalog
    ../../external/cJSON
    ${CMAKE_CURRENT_SOURCE_DIR}/../../external/cJSON
)
