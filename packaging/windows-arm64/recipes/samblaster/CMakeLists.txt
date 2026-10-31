cmake_minimum_required(VERSION 3.24)
project(wgse_samblaster_arm64 VERSION 0.1.26 LANGUAGES C CXX)
if(NOT MSVC OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8 OR NOT WGSE_SAMBLASTER_SOURCE)
    message(FATAL_ERROR "samblaster requires clang-cl ARM64 and WGSE_SAMBLASTER_SOURCE")
endif()
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
set(WGSE_WINDOWS "${CMAKE_CURRENT_LIST_DIR}/../../../windows")
add_executable(samblaster
    "${WGSE_SAMBLASTER_SOURCE}/samblaster.cpp"
    "${WGSE_SAMBLASTER_SOURCE}/sbhash.cpp"
    "${WGSE_WINDOWS}/compat/wgse_compat.c"
    "${CMAKE_CURRENT_LIST_DIR}/../bwa/msvc_time.c"
)
target_compile_definitions(samblaster PRIVATE
    BUILDNUM=26 WIN32_LEAN_AND_MEAN _CRT_SECURE_NO_WARNINGS _CRT_NONSTDC_NO_WARNINGS)
target_compile_options(samblaster PRIVATE
    /UNDEBUG /clang:-O3 /clang:-fsigned-char
    /FI"${CMAKE_CURRENT_LIST_DIR}/msvc_compat.h")
target_include_directories(samblaster PRIVATE
    "${CMAKE_CURRENT_LIST_DIR}/../bwa/include"
    "${WGSE_WINDOWS}/compat/include")
target_link_libraries(samblaster PRIVATE psapi)
install(TARGETS samblaster RUNTIME DESTINATION Library/bin)
