cmake_minimum_required(VERSION 3.24)
project(wgse_bwa_mem2_arm64 VERSION 2.3 LANGUAGES C CXX)
if(NOT MSVC OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR "BWA-MEM2 requires clang-cl ARM64")
endif()
if(NOT WGSE_MEM2_SOURCE)
    message(FATAL_ERROR "WGSE_MEM2_SOURCE is required")
endif()
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
find_package(ZLIB REQUIRED)
find_package(PThreads4W REQUIRED)
find_package(getopt CONFIG REQUIRED)
set(WGSE_WINDOWS "${CMAKE_CURRENT_LIST_DIR}/../../../windows")
set(CORE_NAMES
    fastmap bwtindex utils memcpy_bwamem kthread kstring ksw bntseq bwamem
    profiling bandedSWA FMI_search read_index_ele bwamem_pair kswv bwa
    bwamem_extra kopen
)
foreach(name IN LISTS CORE_NAMES)
    list(APPEND CORE_SOURCES "${WGSE_MEM2_SOURCE}/src/${name}.cpp")
endforeach()
add_library(mem2_core STATIC ${CORE_SOURCES}
    "${WGSE_WINDOWS}/compat/wgse_compat.c"
    "${CMAKE_CURRENT_LIST_DIR}/../bwa/msvc_time.c"
)
target_compile_definitions(mem2_core PUBLIC
    ENABLE_PREFETCH V17=1 MATE_SORT=0 SAIS=1 __SSE2__=1 __SSE4_1__=1
    WIN32 __PTW32_STATIC_LIB _CRT_SECURE_NO_WARNINGS _CRT_NONSTDC_NO_WARNINGS
    _USE_MATH_DEFINES PACKAGE_VERSION="2.3"
)
target_compile_options(mem2_core PUBLIC /FI"${CMAKE_CURRENT_LIST_DIR}/msvc_compat.h")
target_include_directories(mem2_core PUBLIC
    "${CMAKE_CURRENT_LIST_DIR}/include"
    "${CMAKE_CURRENT_LIST_DIR}/../bwa/include"
    "${WGSE_WINDOWS}/compat/include"
    "${WGSE_WINDOWS}/recipes/bwa-mem2/include"
    "${WGSE_MEM2_SOURCE}/sse2neon"
    "${WGSE_MEM2_SOURCE}/index64"
    "${WGSE_MEM2_SOURCE}/src"
)
set_source_files_properties("${WGSE_MEM2_SOURCE}/src/bandedSWA.cpp"
    PROPERTIES COMPILE_OPTIONS "/showIncludes")
target_link_libraries(mem2_core PUBLIC
    ZLIB::ZLIB PThreads4W::PThreads4W getopt::getopt_static psapi
)
add_executable(bwa_mem2 "${WGSE_MEM2_SOURCE}/src/main.cpp")
set_target_properties(bwa_mem2 PROPERTIES OUTPUT_NAME bwa-mem2)
target_link_libraries(bwa_mem2 PRIVATE mem2_core)
add_executable(mem2_io_test "${WGSE_MEM2_SOURCE}/index64/mem2_io.cpp")
target_link_libraries(mem2_io_test PRIVATE mem2_core)
install(TARGETS bwa_mem2 RUNTIME DESTINATION Library/bin)
