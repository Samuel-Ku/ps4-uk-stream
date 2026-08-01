cmake_minimum_required(VERSION 3.16)
project(cs_catalog_standalone CXX C)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

enable_testing()

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
        ${CMAKE_CURRENT_SOURCE_DIR}/../../external/cJSON/cJSON.c
    )
    target_include_directories(${name} PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/../../src/catalog
        ${CMAKE_CURRENT_SOURCE_DIR}/../../external/cJSON
    )
    target_compile_options(${name} PRIVATE -Wall -Wextra)
    add_test(NAME ${name} COMMAND ${name})
endfunction()

c2d_add_catalog_test(test_json)
c2d_add_catalog_test_with_api(test_catalog_api)
