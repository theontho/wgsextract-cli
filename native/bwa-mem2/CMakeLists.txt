find_package(ZLIB REQUIRED)
find_package(Threads REQUIRED)

FetchContent_Declare(mem2
    URL https://github.com/bwa-mem2/bwa-mem2/archive/refs/tags/v2.3.tar.gz
    URL_HASH SHA256=2f240f37d25e483d2ad403cd85f85f47f1030bf43bae184fe699a79a186e609b
    SOURCE_SUBDIR unused)
FetchContent_Declare(neon
    URL https://github.com/DLTcollab/sse2neon/archive/refs/tags/v1.8.0.tar.gz
    URL_HASH SHA256=e251746e3b761f3f0de1ad462b1efe53532341b6b0498d394765fceb85ce8a46
    SOURCE_SUBDIR unused)
FetchContent_Declare(safe
    URL https://github.com/intel/safestringlib/archive/245c4b8cff1d2e7338b7f3a82828fc8e72b29549.tar.gz
    URL_HASH SHA256=6dea4f4accdc14eea706c88bb11725e8c2cd69b0d8c5f5ee19a0669d1883a901)
FetchContent_MakeAvailable(mem2 neon safe)
set_target_properties(safestring_shared PROPERTIES EXCLUDE_FROM_ALL TRUE)
target_compile_options(safestring_objlib PRIVATE
    -include "${CMAKE_CURRENT_SOURCE_DIR}/safe_compat.h")

set(generated "${CMAKE_CURRENT_BINARY_DIR}/generated")
file(MAKE_DIRECTORY "${generated}/intrinsics")
foreach(header emmintrin.h immintrin.h smmintrin.h tmmintrin.h xmmintrin.h x86intrin.h)
    file(WRITE "${generated}/intrinsics/${header}" "#include \"sse2neon.h\"\n")
endforeach()
# Build from a patched copy so quote-included upstream headers can be changed.
set(mem2_src "${generated}/mem2-src")
file(COPY "${mem2_SOURCE_DIR}/src/" DESTINATION "${mem2_src}")
set(WGSE_BATCH_SIZE 2048 CACHE STRING "Reads per BWA-MEM2 worker batch")
file(READ "${mem2_SOURCE_DIR}/src/macro.h" macro_source)
string(REGEX REPLACE "#define BATCH_SIZE 512" "#define BATCH_SIZE ${WGSE_BATCH_SIZE}"
    patched_macro "${macro_source}")
if(patched_macro STREQUAL macro_source AND NOT WGSE_BATCH_SIZE EQUAL 512)
    message(FATAL_ERROR "Unexpected upstream BWA-MEM2 BATCH_SIZE definition")
endif()
file(WRITE "${mem2_src}/macro.h" "${patched_macro}")
file(READ "${mem2_SOURCE_DIR}/src/bandedSWA.cpp" banded_source)
string(REGEX MATCHALL "void BandedPairWiseSW::getScores(8|16)\\(" batch_seams "${banded_source}")
list(LENGTH batch_seams seam_count)
if(NOT seam_count EQUAL 6)
    message(FATAL_ERROR "Unexpected upstream BWA-MEM2 batch interface")
endif()
string(REGEX REPLACE
    "(void BandedPairWiseSW::getScores(8|16)\\([^{]+\\{)"
    "\\1\n    WgseExtensionScope wgse_scope(numPairs);\n    if (wgse_metal_extend(pairArray, seqBufRef, seqBufQer, numPairs, w,\n        o_del, e_del, o_ins, e_ins, zdrop, end_bonus, mat, \\2)) return;\n"
    banded_source "${banded_source}")
file(WRITE "${mem2_src}/bandedSWA.cpp" "#include \"metal_backend.h\"\n${banded_source}")
file(READ "${mem2_SOURCE_DIR}/src/bwamem.cpp" bwamem_source)
include(extension_pipeline.cmake)
file(WRITE "${mem2_src}/bwamem.cpp" "#include \"metal_backend.h\"\n${bwamem_source}")
file(READ "${mem2_SOURCE_DIR}/src/fastmap.cpp" fastmap_source)
string(REGEX REPLACE
    "void __cpuid\\(unsigned int i, unsigned int cpuid\\[4\\]\\) \\{[^}]+\\}"
    "void __cpuid(unsigned int, unsigned int cpuid[4]) {\n    memset(cpuid, 0, 4 * sizeof(unsigned int));\n}"
    fastmap_source "${fastmap_source}")
file(WRITE "${mem2_src}/fastmap.cpp" "${fastmap_source}")
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/extension.metal" kernel_source)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/extension_tiled.metal" tiled_source)
string(APPEND kernel_source "\n${tiled_source}")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/extension.metal"
    "${CMAKE_CURRENT_SOURCE_DIR}/extension_tiled.metal")
configure_file(kernel_source.h.in "${generated}/kernel_source.h" @ONLY)

set(core_names
    bwtindex utils memcpy_bwamem kthread kstring ksw bntseq bwamem
    profiling FMI_search read_index_ele bwamem_pair kswv bwa bwamem_extra kopen)
foreach(name IN LISTS core_names)
    list(APPEND core_sources "${mem2_src}/${name}.cpp")
endforeach()
add_library(mem2_core STATIC ${core_sources}
    "${mem2_src}/bandedSWA.cpp" "${mem2_src}/fastmap.cpp")
target_compile_definitions(mem2_core PUBLIC
    ENABLE_PREFETCH V17=1 MATE_SORT=0 SAIS=1 __SSE2__=1 __SSE4_1__=1)
target_include_directories(mem2_core PUBLIC
    "${generated}/intrinsics" "${CMAKE_CURRENT_SOURCE_DIR}")
target_include_directories(mem2_core SYSTEM PUBLIC
    "${neon_SOURCE_DIR}" "${mem2_src}" "${safe_SOURCE_DIR}/include")
target_compile_options(mem2_core PRIVATE -Wno-register -Wno-unused-result)
target_compile_options(mem2_core PUBLIC
    -include "${CMAKE_CURRENT_SOURCE_DIR}/arm_compat.h" "-Wno-error=#warnings")
target_link_libraries(mem2_core PUBLIC safestring_static ZLIB::ZLIB Threads::Threads)

add_library(metal_backend STATIC metal_backend.mm)
target_include_directories(metal_backend PRIVATE "${generated}")
target_compile_options(metal_backend PRIVATE -fobjc-arc -Wall -Wextra -Werror)
target_link_libraries(metal_backend PUBLIC mem2_core "-framework Foundation" "-framework Metal")
add_executable(wgse-bwa-mem2 main.cpp)
target_link_libraries(wgse-bwa-mem2 PRIVATE metal_backend)
target_compile_definitions(wgse-bwa-mem2 PRIVATE WGSE_VERSION="${PROJECT_VERSION}")
target_compile_options(wgse-bwa-mem2 PRIVATE -Wall -Wextra -Werror)

if(BUILD_TESTING)
    target_compile_definitions(metal_backend PUBLIC WGSE_METAL_BENCHMARK_TOOLS=1)
    add_executable(extension_test tests/extension_test.cpp)
    target_link_libraries(extension_test PRIVATE metal_backend)
    target_compile_options(extension_test PRIVATE -Wall -Wextra -Werror)
    add_test(NAME metal_extension_parity COMMAND extension_test)
    add_test(NAME metal_tiled_parity COMMAND extension_test tiled)
    add_test(NAME metal_striped_parity COMMAND extension_test striped)
    add_test(NAME bwa_mem2_capabilities COMMAND wgse-bwa-mem2 capabilities --json)
    add_test(NAME bwa_mem2_smoke COMMAND "${PROJECT_SOURCE_DIR}/tests/smoke.sh"
        $<TARGET_FILE:wgse-bwa-mem2> "${CMAKE_CURRENT_BINARY_DIR}/smoke" force-gpu)
    add_executable(extension_benchmark tests/extension_benchmark.cpp)
    target_link_libraries(extension_benchmark PRIVATE metal_backend)
    target_compile_options(extension_benchmark PRIVATE -Wall -Wextra -Werror)
    set(WGSE_REPLAY_DIR "" CACHE PATH "Optional captured extension parity fixtures")
    if(WGSE_REPLAY_DIR)
        add_test(NAME metal_real_batch_parity COMMAND extension_benchmark
            --replay "${WGSE_REPLAY_DIR}" --limit 64 --kernel row)
        add_test(NAME metal_tiled_real_batch_parity COMMAND extension_benchmark
            --replay "${WGSE_REPLAY_DIR}" --limit 64 --kernel tiled)
        add_test(NAME metal_striped_real_batch_parity COMMAND extension_benchmark
            --replay "${WGSE_REPLAY_DIR}" --limit 64 --kernel striped --workers 8)
    endif()
    # Hosted CI runners may lack a usable Metal device; those tests exit 77.
    get_property(tests DIRECTORY PROPERTY TESTS)
    set_tests_properties(${tests} PROPERTIES SKIP_RETURN_CODE 77)
endif()

install(TARGETS wgse-bwa-mem2 RUNTIME DESTINATION bin)
install(FILES "${mem2_SOURCE_DIR}/LICENSE" DESTINATION share/licenses/wgse-bwa-mem2
    RENAME BWA-MEM2-LICENSE)
install(FILES "${neon_SOURCE_DIR}/LICENSE" DESTINATION share/licenses/wgse-bwa-mem2
    RENAME sse2neon-LICENSE)
install(FILES "${safe_SOURCE_DIR}/LICENSE" DESTINATION share/licenses/wgse-bwa-mem2
    RENAME safestringlib-LICENSE)
