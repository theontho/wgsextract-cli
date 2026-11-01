cmake_minimum_required(VERSION 3.24)
project(wgse_htslib_arm64 VERSION 1.24 LANGUAGES C)
option(WGSE_HTSLIB_TOOLS "Build and install standalone HTS tools" ON)
if(NOT MSVC OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8 OR NOT WGSE_HTSLIB_SOURCE)
    message(FATAL_ERROR "HTS qualification requires clang-cl ARM64 and WGSE_HTSLIB_SOURCE")
endif()
file(TO_CMAKE_PATH "${WGSE_HTSLIB_SOURCE}" WGSE_HTSLIB_SOURCE)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
find_package(ZLIB REQUIRED)
find_package(BZip2 REQUIRED)
find_package(LibLZMA REQUIRED)
find_package(libdeflate CONFIG REQUIRED)
find_package(unofficial-tre CONFIG REQUIRED)
find_package(PThreads4W REQUIRED)
find_package(getopt CONFIG REQUIRED)
set(WGSE_WINDOWS "${CMAKE_CURRENT_LIST_DIR}/../../../windows")
configure_file(config.h "${WGSE_HTSLIB_SOURCE}/config.h" COPYONLY)
set(WGSE_HTS_CC "${CMAKE_C_COMPILER_ID} ${CMAKE_C_COMPILER_VERSION} ARM64")
configure_file(config_vars.h.in "${WGSE_HTSLIB_SOURCE}/config_vars.h" @ONLY)
file(WRITE "${WGSE_HTSLIB_SOURCE}/version.h" "#define HTS_VERSION_TEXT \"1.24\"\n")
set(CORE_NAMES
    kfunc kstring bcf_sr_sort bgzf errmod faidx header hfile hts hts_expr hts_os
    md5 multipart probaln realn regidx region sam sam_mods simd synced_bcf_reader
    vcf_sweep tbx textutils thread_pool vcf vcfutils
    cram/cram_codecs cram/cram_decode cram/cram_encode cram/cram_external
    cram/cram_index cram/cram_io cram/cram_stats cram/mFILE cram/open_trace_file
    cram/pooled_alloc cram/string_alloc
)
foreach(name IN LISTS CORE_NAMES)
    list(APPEND CORE_SOURCES "${WGSE_HTSLIB_SOURCE}/${name}.c")
endforeach()
set(CODEC_NAMES
    arith_dynamic fqzcomp_qual htscodecs pack rANS_static4x16pr
    rANS_static32x16pr_neon rANS_static32x16pr rANS_static rle tokenise_name3 utils
)
foreach(name IN LISTS CODEC_NAMES)
    list(APPEND CORE_SOURCES "${WGSE_HTSLIB_SOURCE}/htscodecs/htscodecs/${name}.c")
endforeach()
add_library(hts STATIC ${CORE_SOURCES}
    "${CMAKE_CURRENT_LIST_DIR}/../bwa/msvc_time.c")
target_compile_definitions(hts PUBLIC
    WIN32 WIN32_LEAN_AND_MEAN __ARM_NEON=1 __PTW32_STATIC_LIB
    _CRT_SECURE_NO_WARNINGS _CRT_NONSTDC_NO_WARNINGS _USE_MATH_DEFINES
)
target_compile_options(hts PUBLIC
    /UNDEBUG /clang:-fsigned-char /FI"${CMAKE_CURRENT_LIST_DIR}/msvc_compat.h")
target_include_directories(hts PUBLIC
    "${CMAKE_CURRENT_LIST_DIR}/include"
    "${CMAKE_CURRENT_LIST_DIR}/../bwa/include"
    "${WGSE_WINDOWS}/compat/include"
    "${WGSE_HTSLIB_SOURCE}"
)
target_link_libraries(hts PUBLIC
    ZLIB::ZLIB BZip2::BZip2 LibLZMA::LibLZMA libdeflate::libdeflate_static
    unofficial::tre::tre PThreads4W::PThreads4W getopt::getopt_static ws2_32 psapi bcrypt
)
# All CRAM consumers need more than MSVC's default 1 MiB codec stack.
target_link_options(hts INTERFACE /STACK:8388608)
set_source_files_properties(
    "${WGSE_HTSLIB_SOURCE}/htscodecs/htscodecs/rANS_static32x16pr_neon.c"
    PROPERTIES COMPILE_OPTIONS "/showIncludes")
if(WGSE_HTSLIB_TOOLS)
    foreach(name IN ITEMS bgzip tabix htsfile)
        add_executable(${name} "${WGSE_HTSLIB_SOURCE}/${name}.c"
            "${WGSE_WINDOWS}/compat/wgse_compat.c")
        target_link_libraries(${name} PRIVATE hts)
        install(TARGETS ${name} RUNTIME DESTINATION Library/bin)
    endforeach()
    install(TARGETS hts ARCHIVE DESTINATION Library/lib)
    install(DIRECTORY "${WGSE_HTSLIB_SOURCE}/htslib" DESTINATION Library/include
        FILES_MATCHING PATTERN "*.h")
endif()
foreach(name IN ITEMS cram_stop bam_seq cram_buffers)
    add_executable(${name}_test "${WGSE_WINDOWS}/tests/${name}.c"
        "${WGSE_WINDOWS}/compat/wgse_compat.c")
    target_link_libraries(${name}_test PRIVATE hts)
endforeach()
add_executable(msvc_files_test "${CMAKE_CURRENT_LIST_DIR}/../../tests/msvc_files.c")
target_include_directories(msvc_files_test PRIVATE "${CMAKE_CURRENT_LIST_DIR}/../../compat")
target_link_libraries(msvc_files_test PRIVATE hts)
