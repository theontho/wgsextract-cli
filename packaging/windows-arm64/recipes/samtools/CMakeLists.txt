cmake_minimum_required(VERSION 3.24)
project(wgse_samtools_arm64 VERSION 1.24 LANGUAGES C)
if(NOT WGSE_SAMTOOLS_SOURCE)
    message(FATAL_ERROR "WGSE_SAMTOOLS_SOURCE is required")
endif()
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
set(WGSE_HTSLIB_SOURCE "${WGSE_SAMTOOLS_SOURCE}/htslib-1.24")
set(WGSE_HTSLIB_TOOLS OFF CACHE BOOL "" FORCE)
add_subdirectory("${CMAKE_CURRENT_LIST_DIR}/../htslib" hts-build)
file(WRITE "${WGSE_SAMTOOLS_SOURCE}/config.h" "/* Curses disabled, as on Windows x64. */\n")
configure_file(version.h.in "${WGSE_SAMTOOLS_SOURCE}/version.h" @ONLY)
set(APP_NAMES
    bam_aux bam_index bam_plcmd sam_view bam_fastq bam_cat bam_md bam_plbuf
    bam_reheader bam_sort bam_rmdup bam_rmdupse bam_mate bam_stat bam_color
    bamtk bam2bcf sample cut_target phase bam2depth coverage padding bedcov
    bamshuf faidx dict stats stats_isize bam_flags bam_split bam_tview
    bam_tview_curses bam_tview_html bam_lpileup bam_quickcheck bam_addrprg
    bam_markdup tmp_file bam_ampliconclip amplicon_stats bam_import bam_samples
    bam_consensus consensus_pileup reference reset cram_size bam_checksum
    sam_opts sam_utils bedidx bam
)
foreach(name IN LISTS APP_NAMES)
    list(APPEND APP_SOURCES "${WGSE_SAMTOOLS_SOURCE}/${name}.c")
endforeach()
set_source_files_properties("${WGSE_SAMTOOLS_SOURCE}/stats_isize.c"
    PROPERTIES COMPILE_OPTIONS "/FI${CMAKE_CURRENT_LIST_DIR}/../../compat/msvc_stats_isize.h")
add_executable(samtools ${APP_SOURCES}
    "${WGSE_SAMTOOLS_SOURCE}/lz4/lz4.c"
    "${CMAKE_CURRENT_LIST_DIR}/../../../windows/compat/wgse_compat.c")
target_include_directories(samtools PRIVATE "${WGSE_SAMTOOLS_SOURCE}")
target_compile_definitions(samtools PRIVATE NOMINMAX)
target_link_libraries(samtools PRIVATE hts)
install(TARGETS samtools RUNTIME DESTINATION Library/bin)
