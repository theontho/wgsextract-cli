cmake_minimum_required(VERSION 3.24)
project(wgse_fastp_arm64 VERSION 1.4.0 LANGUAGES C CXX)
if(NOT MSVC OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8 OR NOT WGSE_FASTP_SOURCE)
    message(FATAL_ERROR "fastp requires clang-cl ARM64 and WGSE_FASTP_SOURCE")
endif()
file(TO_CMAKE_PATH "${WGSE_FASTP_SOURCE}" WGSE_FASTP_SOURCE)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
find_package(hwy CONFIG REQUIRED)
find_package(libdeflate CONFIG REQUIRED)
find_package(PThreads4W REQUIRED)
find_package(getopt CONFIG REQUIRED)
set(ISAL "${WGSE_FASTP_SOURCE}/isa-l")
set(ISAL_NAMES
    igzip igzip_base igzip_icf_base igzip_icf_body igzip_inflate
    hufftables_c huff_codes adler32_base flatten_ll encode_df
    igzip_base_aliases proc_heap_base
)
foreach(name IN LISTS ISAL_NAMES)
    list(APPEND ISAL_SOURCES "${ISAL}/igzip/${name}.c")
endforeach()
add_library(isal_native STATIC ${ISAL_SOURCES}
    "${ISAL}/crc/crc_base.c" "${ISAL}/crc/crc64_base.c"
    "${ISAL}/crc/crc_base_aliases.c")
target_include_directories(isal_native PRIVATE "${ISAL}/include" "${ISAL}/igzip")
target_compile_definitions(isal_native PRIVATE ISAL_DEPRECATED_INTERNAL)
target_compile_options(isal_native PRIVATE /UNDEBUG /clang:-O3 /FIintrin.h)
file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/include/isa-l")
file(COPY "${ISAL}/include/igzip_lib.h" "${ISAL}/include/isal_api.h"
    DESTINATION "${CMAKE_CURRENT_BINARY_DIR}/include/isa-l")
file(GLOB FASTP_SOURCES CONFIGURE_DEPENDS "${WGSE_FASTP_SOURCE}/src/*.cpp")
set(WGSE_WINDOWS "${CMAKE_CURRENT_LIST_DIR}/../../../windows")
add_executable(fastp ${FASTP_SOURCES}
    "${WGSE_WINDOWS}/compat/wgse_compat.c"
    "${CMAKE_CURRENT_LIST_DIR}/../bwa/msvc_time.c")
target_compile_definitions(fastp PRIVATE
    HWY_STATIC_DEFINE WIN32_LEAN_AND_MEAN NOMINMAX __PTW32_STATIC_LIB
    _USE_MATH_DEFINES _CRT_SECURE_NO_WARNINGS _CRT_NONSTDC_NO_WARNINGS)
target_compile_options(fastp PRIVATE /UNDEBUG /clang:-O3 /clang:-fsigned-char
    /FI"${CMAKE_CURRENT_LIST_DIR}/msvc_compat.h" /showIncludes)
target_include_directories(fastp PRIVATE
    "${CMAKE_CURRENT_BINARY_DIR}/include"
    "${WGSE_FASTP_SOURCE}" "${WGSE_FASTP_SOURCE}/inc"
    "${CMAKE_CURRENT_LIST_DIR}/../bwa/include"
    "${WGSE_WINDOWS}/compat/include")
target_link_libraries(fastp PRIVATE
    isal_native hwy::hwy libdeflate::libdeflate_static
    PThreads4W::PThreads4W getopt::getopt_static psapi)
add_executable(fastp_cmdline_test
    "${CMAKE_CURRENT_LIST_DIR}/../../tests/fastp_cmdline.cpp")
target_include_directories(fastp_cmdline_test PRIVATE "${WGSE_FASTP_SOURCE}/src")
target_compile_options(fastp_cmdline_test PRIVATE /UNDEBUG)
install(TARGETS fastp RUNTIME DESTINATION Library/bin)
