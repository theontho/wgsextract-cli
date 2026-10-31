cmake_minimum_required(VERSION 3.24)
project(wgse_minimap2_arm64 C)

option(arm_neon "Compile upstream ARM NEON kernels" ON)
if(NOT MSVC OR NOT arm_neon OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR "minimap2 qualification requires clang-cl ARM64 and arm_neon=1")
endif()
if(NOT WGSE_MINIMAP2_SOURCE)
    message(FATAL_ERROR "WGSE_MINIMAP2_SOURCE is required")
endif()
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
find_package(ZLIB REQUIRED)
find_package(PThreads4W REQUIRED)

set(WGSE_WINDOWS "${CMAKE_CURRENT_LIST_DIR}/../../../windows")
set(CORE_NAMES
    kthread kalloc misc bseq sketch sdust options index lchain align hit seed
    jump map format pe esterr splitidx ksw2_ll_sse
)
foreach(name IN LISTS CORE_NAMES)
    list(APPEND CORE_SOURCES "${WGSE_MINIMAP2_SOURCE}/${name}.c")
endforeach()
set(NEON_NAMES ksw2_extz2_sse ksw2_extd2_sse ksw2_exts2_sse)
foreach(name IN LISTS NEON_NAMES)
    set(source "${WGSE_MINIMAP2_SOURCE}/${name}.c")
    list(APPEND CORE_SOURCES "${source}")
    # These upstream selectors activate the SSE API translated by sse2neon;
    # they do not change clang-cl's ARM64 code-generation target.
    set_source_files_properties("${source}" PROPERTIES
        COMPILE_DEFINITIONS "__SSE2__=1;KSW_SSE2_ONLY=1"
        COMPILE_OPTIONS "/showIncludes"
    )
endforeach()
add_library(minimap2_core STATIC ${CORE_SOURCES})
target_compile_definitions(minimap2_core PUBLIC
    HAVE_KALLOC WIN32 __PTW32_STATIC_LIB
    _CRT_SECURE_NO_WARNINGS _CRT_NONSTDC_NO_WARNINGS _USE_MATH_DEFINES
)
target_compile_options(minimap2_core PUBLIC
    /FI"${CMAKE_CURRENT_LIST_DIR}/msvc_compat.h"
    /clang:-fsigned-char
)
target_include_directories(minimap2_core PUBLIC
    "${CMAKE_CURRENT_LIST_DIR}/include"
    "${WGSE_WINDOWS}/compat/include"
    "${WGSE_MINIMAP2_SOURCE}/sse2neon"
    "${WGSE_MINIMAP2_SOURCE}"
)
target_link_libraries(minimap2_core PUBLIC ZLIB::ZLIB PThreads4W::PThreads4W psapi)
add_executable(minimap2
    "${WGSE_MINIMAP2_SOURCE}/main.c"
    "${WGSE_WINDOWS}/compat/wgse_compat.c"
)
target_link_libraries(minimap2 PRIVATE minimap2_core)
install(TARGETS minimap2 RUNTIME DESTINATION Library/bin)
