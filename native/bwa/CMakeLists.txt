find_package(ZLIB REQUIRED)
find_package(Threads REQUIRED)

FetchContent_Declare(bwa
    URL https://github.com/lh3/bwa/archive/refs/tags/v0.7.19.tar.gz
    URL_HASH SHA256=cdff5db67652c5b805a3df08c4e813a822c65791913eccfb3cf7d528588f37bc
    SOURCE_SUBDIR unused)
FetchContent_MakeAvailable(bwa)

# Bump when the wgse-bwa package changes; it is versioned separately from BWA.
set(WGSE_BWA_VERSION 0.6.0)
set(generated "${CMAKE_CURRENT_BINARY_DIR}/generated")
file(MAKE_DIRECTORY "${generated}")
include("${CMAKE_CURRENT_SOURCE_DIR}/upstream_seams.cmake")

# Mirrors upstream's Makefile LOBJS, AOBJS and main.o.
set(bwa_names
    utils kthread kstring ksw bwt bntseq bwa bwamem bwamem_pair bwamem_extra
    malloc_wrap QSufSort bwt_gen rope rle is bwtindex
    bwashm bwase bwaseqio bwtgap bwtaln bamlite bwape kopen pemerge maxk
    bwtsw2_core bwtsw2_main bwtsw2_aux bwt_lite bwtsw2_chain fastmap bwtsw2_pair
    main)
foreach(name IN LISTS bwa_names)
    if(name IN_LIST patched_sources)
        list(APPEND bwa_sources "${generated}/${name}.c")
    else()
        list(APPEND bwa_sources "${bwa_SOURCE_DIR}/${name}.c")
    endif()
endforeach()

file(READ seeding_types.h seeding_types)
file(READ seeding.metal seeding_source)
file(READ rescue_types.h rescue_types)
file(READ rescue.metal rescue_source)
file(READ extend_types.h extend_types)
file(READ extend.metal extend_source)
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/extend_types.h" "${CMAKE_CURRENT_SOURCE_DIR}/extend.metal"
    "${CMAKE_CURRENT_SOURCE_DIR}/seeding_types.h" "${CMAKE_CURRENT_SOURCE_DIR}/seeding.metal"
    "${CMAKE_CURRENT_SOURCE_DIR}/rescue_types.h" "${CMAKE_CURRENT_SOURCE_DIR}/rescue.metal")
configure_file(seeding_source.h.in "${generated}/seeding_source.h" @ONLY)

# BWA plus the Metal seeding backend, shared by the executable and the tests.
add_library(wgse_bwa_core STATIC ${bwa_sources} metal_backend.mm)
target_include_directories(wgse_bwa_core PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}" "${generated}")
target_include_directories(wgse_bwa_core SYSTEM PUBLIC "${bwa_SOURCE_DIR}")
target_compile_definitions(wgse_bwa_core PRIVATE
    $<$<COMPILE_LANGUAGE:C>:HAVE_PTHREAD USE_MALLOC_WRAPPERS
    PACKAGE_VERSION="0.7.19-r1273-wgse-${WGSE_BWA_VERSION}">)
target_compile_options(wgse_bwa_core PRIVATE
    $<$<COMPILE_LANGUAGE:C>:-Wno-unused-function -Wno-unused-but-set-variable
    -Wno-deprecated-non-prototype>
    $<$<COMPILE_LANGUAGE:OBJCXX>:-fobjc-arc -Wall -Wextra -Werror>)
target_link_libraries(wgse_bwa_core PUBLIC wgse_metal_common ZLIB::ZLIB Threads::Threads m)

add_executable(wgse-bwa main.mm)
target_compile_definitions(wgse-bwa PRIVATE WGSE_VERSION="${WGSE_BWA_VERSION}")
target_compile_options(wgse-bwa PRIVATE -fobjc-arc -Wall -Wextra -Werror)
target_link_libraries(wgse-bwa PRIVATE wgse_bwa_core)

if(BUILD_TESTING)
    # Hidden routing and failure-injection flags for tests and benchmarks only.
    target_compile_definitions(wgse-bwa PRIVATE WGSE_METAL_BENCHMARK_TOOLS=1)
    # Bounds-checked libc++ containers make out-of-range indexing in the backend trap.
    target_compile_definitions(wgse_bwa_core PRIVATE
        $<$<COMPILE_LANGUAGE:OBJCXX>:_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_FAST>)
    add_test(NAME bwa_capabilities COMMAND wgse-bwa capabilities --json)
    set_tests_properties(bwa_capabilities PROPERTIES
        PASS_REGULAR_EXPRESSION "\"engine\":\"bwa\".*\"metal_hardware_supported\":")
    add_test(NAME bwa_smoke COMMAND "${PROJECT_SOURCE_DIR}/tests/smoke.sh"
        $<TARGET_FILE:wgse-bwa> "${CMAKE_CURRENT_BINARY_DIR}/smoke" force-gpu)
    add_executable(seeding_benchmark tests/seeding_benchmark.mm)
    target_compile_options(seeding_benchmark PRIVATE -fobjc-arc -Wall -Wextra -Werror)
    target_link_libraries(seeding_benchmark PRIVATE wgse_bwa_core)
    add_test(NAME bwa_metal_seeding_parity
        COMMAND "${CMAKE_CURRENT_SOURCE_DIR}/tests/seeding_parity.sh"
        $<TARGET_FILE:wgse-bwa> $<TARGET_FILE:seeding_benchmark>
        "${CMAKE_CURRENT_BINARY_DIR}/seeding-parity")
    add_test(NAME bwa_metal_rescue_parity
        COMMAND "${CMAKE_CURRENT_SOURCE_DIR}/tests/rescue_parity.sh"
        $<TARGET_FILE:wgse-bwa> "${CMAKE_CURRENT_BINARY_DIR}/rescue-parity")
    add_test(NAME bwa_metal_extend_parity
        COMMAND "${CMAKE_CURRENT_SOURCE_DIR}/tests/extend_parity.sh"
        $<TARGET_FILE:wgse-bwa> "${CMAKE_CURRENT_BINARY_DIR}/extend-parity")
    # Exit 77 means the machine has no unified-memory Metal device.
    set_tests_properties(bwa_smoke bwa_metal_seeding_parity bwa_metal_rescue_parity
        bwa_metal_extend_parity PROPERTIES SKIP_RETURN_CODE 77)
endif()

install(TARGETS wgse-bwa RUNTIME DESTINATION bin)
install(FILES "${CMAKE_CURRENT_SOURCE_DIR}/../../LICENSE" DESTINATION share/licenses/wgse-bwa
    RENAME WGSE-LICENSE)
install(FILES "${bwa_SOURCE_DIR}/COPYING" DESTINATION share/licenses/wgse-bwa
    RENAME BWA-LICENSE)
install(FILES SEEDING-LICENSE DESTINATION share/licenses/wgse-bwa
    RENAME METAL-SEEDING-LICENSE)
