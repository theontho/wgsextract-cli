cmake_minimum_required(VERSION 3.24)
project(wgse_bcftools_arm64 VERSION 1.24 LANGUAGES C)
if(NOT WGSE_BCFTOOLS_SOURCE)
    message(FATAL_ERROR "WGSE_BCFTOOLS_SOURCE is required")
endif()
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
set(WGSE_HTSLIB_SOURCE "${WGSE_BCFTOOLS_SOURCE}/htslib-1.24")
set(WGSE_HTSLIB_TOOLS OFF CACHE BOOL "" FORCE)
add_subdirectory("${CMAKE_CURRENT_LIST_DIR}/../htslib" hts-build)
file(WRITE "${WGSE_BCFTOOLS_SOURCE}/config.h" "/* Static native ARM64; plugins disabled. */\n")
file(WRITE "${WGSE_BCFTOOLS_SOURCE}/version.h" "#define BCFTOOLS_VERSION \"1.24\"\n")
set(APP_NAMES
    main vcfindex tabix vcfstats vcfisec vcfmerge vcfquery vcffilter filter vcfsom
    vcfnorm vcfgtcheck vcfview vcfannotate vcfroh vcfconcat vcfcall mcall vcmp
    gvcf reheader convert vcfconvert tsv2vcf vcfcnv vcfhead HMM consensus
    ploidy bin hclust version regidx smpl_ilist csq vcfbuf mpileup bam2bcf
    bam2bcf_indel bam2bcf_iaux bam2bcf_edlib read_consensus bam_sample vcfsort
    cols extsort dist abuf ccall em prob1 kmin str_finder gff edlib mpileup2/mpileup
)
foreach(name IN LISTS APP_NAMES)
    list(APPEND APP_SOURCES "${WGSE_BCFTOOLS_SOURCE}/${name}.c")
endforeach()
add_executable(bcftools ${APP_SOURCES}
    "${CMAKE_CURRENT_LIST_DIR}/../../../windows/compat/wgse_compat.c")
target_include_directories(bcftools PRIVATE "${WGSE_BCFTOOLS_SOURCE}")
target_compile_definitions(bcftools PRIVATE NOMINMAX)
target_link_libraries(bcftools PRIVATE hts)
install(TARGETS bcftools RUNTIME DESTINATION Library/bin)
