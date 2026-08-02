cmake_minimum_required(VERSION 3.16)
project(cs_catalog_standalone CXX C)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

enable_testing()

# CatalogApi uses std::thread / std::mutex / std::condition_variable.
find_package(Threads REQUIRED)

function(c2d_add_catalog_test name)
    add_executable(${name} ${CMAKE_CURRENT_SOURCE_DIR}/../catalog/${name}.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog/Json.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../../external/cJSON/cJSON.c
    )
    target_include_directories(${name} PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog
        ${CMAKE_CURRENT_SOURCE_DIR}/../../external/cJSON
    )
    target_compile_options(${name} PRIVATE -Wall -Wextra)
    add_test(NAME ${name} COMMAND ${name})
endfunction()

function(c2d_add_catalog_test_with_api name)
    add_executable(${name} ${CMAKE_CURRENT_SOURCE_DIR}/../catalog/${name}.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog/Json.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog/CatalogApi.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog/PosterCache.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../../external/cJSON/cJSON.c
    )
    target_include_directories(${name} PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog
        ${CMAKE_CURRENT_SOURCE_DIR}/../../external/cJSON
    )
    target_compile_options(${name} PRIVATE -Wall -Wextra)
    target_link_libraries(${name} Threads::Threads)
    add_test(NAME ${name} COMMAND ${name})
endfunction()

function(c2d_add_catalog_test_with_fake name)
    # Test file uses a FakeHttpClient (header-only) instead of Browser.
    add_executable(${name} ${CMAKE_CURRENT_SOURCE_DIR}/../catalog/${name}.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog/Json.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog/CatalogApi.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog/PosterCache.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../../external/cJSON/cJSON.c
        ${CMAKE_CURRENT_SOURCE_DIR}/FakeHttpClient.h
    )
    target_include_directories(${name} PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog
        ${CMAKE_CURRENT_SOURCE_DIR}/../../external/cJSON
    )
    target_compile_options(${name} PRIVATE -Wall -Wextra)
    target_link_libraries(${name} Threads::Threads)
    add_test(NAME ${name} COMMAND ${name})
endfunction()

function(c2d_add_catalog_test_widget name)
    add_executable(${name} ${CMAKE_CURRENT_SOURCE_DIR}/../catalog/${name}.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog/OnscreenKeyboard.cpp
    )
    target_include_directories(${name} PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog
    )
    target_compile_options(${name} PRIVATE -Wall -Wextra)
    add_test(NAME ${name} COMMAND ${name})
endfunction()

# Static (no external deps) test — does NOT link any catalog source.
# Used for plumbing helpers (RAII wrappers etc.) that the production
# code can keep in an anonymous namespace without exporting.
function(c2d_add_catalog_test_static name)
    add_executable(${name} ${CMAKE_CURRENT_SOURCE_DIR}/../catalog/${name}.cpp)
    target_compile_options(${name} PRIVATE -Wall -Wextra)
    add_test(NAME ${name} COMMAND ${name})
endfunction()

# Tests that need the disk poster cache in addition to CatalogApi.
function(c2d_add_catalog_test_with_cache name)
    add_executable(${name} ${CMAKE_CURRENT_SOURCE_DIR}/../catalog/${name}.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog/Json.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog/CatalogApi.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog/PosterCache.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/../../external/cJSON/cJSON.c
        ${CMAKE_CURRENT_SOURCE_DIR}/FakeHttpClient.h
    )
    target_include_directories(${name} PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog
        ${CMAKE_CURRENT_SOURCE_DIR}/../../external/cJSON
    )
    target_compile_options(${name} PRIVATE -Wall -Wextra)
    target_link_libraries(${name} Threads::Threads)
    add_test(NAME ${name} COMMAND ${name})
endfunction()

c2d_add_catalog_test(test_json)
c2d_add_catalog_test(test_error_strings)
c2d_add_catalog_test_with_api(test_catalog_api)
# Pure policy helper (ContentItem header) — reuses the API test wiring
# for the catalog include dirs.
c2d_add_catalog_test_with_api(test_memory_policy)
# Issue #62 — ContentItem.sources parsing (chip strip roster).
c2d_add_catalog_test_with_api(test_sources_parsing)
# Issue #63 — search AsyncWithProvider (provider filter chip strip).
c2d_add_catalog_test_with_api(test_search_provider)
# Issue #73 — providersAsync / parseProviders (health snapshot).
c2d_add_catalog_test_with_api(test_providers_parsing)
c2d_add_catalog_test_widget(test_keyboard)
c2d_add_catalog_test_widget(test_keyboard_utf8)
c2d_add_catalog_test_with_fake(test_catalog_api_async)
c2d_add_catalog_test_static(test_tmp_file)
c2d_add_catalog_test_with_cache(test_poster_cache)

# State store test: CatalogState + Json (read path); no HTTP involved.
add_executable(test_catalog_state ${CMAKE_CURRENT_SOURCE_DIR}/../catalog/test_catalog_state.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog/CatalogState.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog/Json.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../../external/cJSON/cJSON.c
)
target_include_directories(test_catalog_state PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog
    ${CMAKE_CURRENT_SOURCE_DIR}/../../external/cJSON
)
target_compile_options(test_catalog_state PRIVATE -Wall -Wextra)
add_test(NAME test_catalog_state COMMAND test_catalog_state)
