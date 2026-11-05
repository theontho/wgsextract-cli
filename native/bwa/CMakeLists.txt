find_package(ZLIB REQUIRED)
find_package(Threads REQUIRED)

FetchContent_Declare(bwa
    URL https://github.com/lh3/bwa/archive/refs/tags/v0.7.19.tar.gz
    URL_HASH SHA256=cdff5db67652c5b805a3df08c4e813a822c65791913eccfb3cf7d528588f37bc
    SOURCE_SUBDIR unused)
FetchContent_MakeAvailable(bwa)

# Mirrors upstream's Makefile LOBJS, AOBJS and main.o.
set(bwa_names
    utils kthread kstring ksw bwt bntseq bwa bwamem bwamem_pair bwamem_extra
    malloc_wrap QSufSort bwt_gen rope rle is bwtindex
    bwashm bwase bwaseqio bwtgap bwtaln bamlite bwape kopen pemerge maxk
    bwtsw2_core bwtsw2_main bwtsw2_aux bwt_lite bwtsw2_chain fastmap bwtsw2_pair
    main)
foreach(name IN LISTS bwa_names)
    list(APPEND bwa_sources "${bwa_SOURCE_DIR}/${name}.c")
endforeach()
add_executable(wgse-bwa ${bwa_sources})
target_compile_definitions(wgse-bwa PRIVATE HAVE_PTHREAD USE_MALLOC_WRAPPERS
    PACKAGE_VERSION="0.7.19-r1273-wgse-${PROJECT_VERSION}")
target_compile_options(wgse-bwa PRIVATE -Wno-unused-function -Wno-unused-but-set-variable
    -Wno-deprecated-non-prototype)
target_link_libraries(wgse-bwa PRIVATE ZLIB::ZLIB Threads::Threads m)

if(BUILD_TESTING)
    add_test(NAME bwa_smoke COMMAND "${PROJECT_SOURCE_DIR}/tests/smoke.sh"
        $<TARGET_FILE:wgse-bwa> "${CMAKE_CURRENT_BINARY_DIR}/smoke")
endif()

install(TARGETS wgse-bwa RUNTIME DESTINATION bin)
install(FILES "${CMAKE_CURRENT_SOURCE_DIR}/../../LICENSE" DESTINATION share/licenses/wgse-bwa
    RENAME WGSE-LICENSE)
install(FILES "${bwa_SOURCE_DIR}/COPYING" DESTINATION share/licenses/wgse-bwa
    RENAME BWA-LICENSE)
