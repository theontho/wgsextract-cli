cmake_minimum_required(VERSION 3.24)
project(wgse_bwa_arm64 C)

if(NOT MSVC OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR "BWA qualification requires MSVC/clang-cl ARM64")
endif()
if(NOT WGSE_BWA_SOURCE)
    message(FATAL_ERROR "WGSE_BWA_SOURCE is required")
endif()
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
find_package(ZLIB REQUIRED)
find_package(PThreads4W REQUIRED)
find_package(getopt CONFIG REQUIRED)

set(WGSE_WINDOWS "${CMAKE_CURRENT_LIST_DIR}/../../../windows")
add_library(bwa_core STATIC
    "${WGSE_BWA_SOURCE}/utils.c"
    "${WGSE_BWA_SOURCE}/kthread.c"
    "${WGSE_BWA_SOURCE}/kstring.c"
    "${WGSE_BWA_SOURCE}/ksw.c"
    "${WGSE_BWA_SOURCE}/bwt.c"
    "${WGSE_BWA_SOURCE}/bntseq.c"
    "${WGSE_BWA_SOURCE}/bwa.c"
    "${WGSE_BWA_SOURCE}/bwamem.c"
    "${WGSE_BWA_SOURCE}/bwamem_pair.c"
    "${WGSE_BWA_SOURCE}/bwamem_extra.c"
    "${WGSE_BWA_SOURCE}/QSufSort.c"
    "${WGSE_BWA_SOURCE}/bwt_gen.c"
    "${WGSE_BWA_SOURCE}/rope.c"
    "${WGSE_BWA_SOURCE}/rle.c"
    "${WGSE_BWA_SOURCE}/is.c"
    "${WGSE_BWA_SOURCE}/bwtindex.c"
    "${WGSE_WINDOWS}/compat/wgse_compat.c"
    msvc_time.c
)
target_compile_definitions(bwa_core PUBLIC
    _CRT_SECURE_NO_WARNINGS _CRT_NONSTDC_NO_WARNINGS _USE_MATH_DEFINES
    __PTW32_STATIC_LIB HAVE_PTHREAD __ARM_NEON=1
)
target_compile_options(bwa_core PUBLIC
    /UNDEBUG
    /FI"${CMAKE_CURRENT_LIST_DIR}/msvc_compat.h"
)
set_source_files_properties("${WGSE_BWA_SOURCE}/ksw.c" PROPERTIES COMPILE_OPTIONS "/showIncludes")
target_include_directories(bwa_core PUBLIC
    "${CMAKE_CURRENT_LIST_DIR}/include"
    "${WGSE_WINDOWS}/compat/include"
    "${WGSE_BWA_SOURCE}"
)
target_link_libraries(bwa_core PUBLIC
    ZLIB::ZLIB PThreads4W::PThreads4W getopt::getopt_static psapi
)

set(BWA_FRONTEND
    bwashm bwase bwaseqio bwtgap bwtaln bamlite bwape kopen pemerge maxk
    bwtsw2_core bwtsw2_main bwtsw2_aux bwt_lite bwtsw2_chain fastmap bwtsw2_pair main
)
foreach(name IN LISTS BWA_FRONTEND)
    list(APPEND BWA_FRONTEND_SOURCES "${WGSE_BWA_SOURCE}/${name}.c")
endforeach()
add_executable(bwa ${BWA_FRONTEND_SOURCES})
target_link_libraries(bwa PRIVATE bwa_core)
add_executable(bwa_sa_test "${WGSE_WINDOWS}/tests/bwa_sa.c")
target_link_libraries(bwa_sa_test PRIVATE bwa_core)
install(TARGETS bwa RUNTIME DESTINATION Library/bin)
