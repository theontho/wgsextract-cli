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
function(mem2_seam variable before after)
    string(FIND "${${variable}}" "${before}" position)
    if(position EQUAL -1)
        message(FATAL_ERROR "Unexpected upstream BWA-MEM2 source: ${before}")
    endif()
    string(REPLACE "${before}" "${after}" patched "${${variable}}")
    set(${variable} "${patched}" PARENT_SCOPE)
endfunction()
# GPU mate rescue, shared with wgse-bwa (native/bwa): BWA-MEM2's mem_sam_pe(),
# mem_matesw() and ksw_align2() are BWA 0.7.19's.
set(bwa_dir "${CMAKE_CURRENT_SOURCE_DIR}/../bwa")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${bwa_dir}/rescue_oracle.c.in" "${bwa_dir}/rescue_types.h" "${bwa_dir}/rescue.metal"
    "${CMAKE_CURRENT_SOURCE_DIR}/mem_stages.cpp.in"
    "${CMAKE_CURRENT_SOURCE_DIR}/fastmap_stages.cpp.in"
    "${CMAKE_CURRENT_SOURCE_DIR}/seeding_types.h" "${CMAKE_CURRENT_SOURCE_DIR}/seeding.metal")
# GPU SMEM collection reads FMI_search's checkpoints in place, so they are
# page-aligned for Metal.
file(READ "${mem2_SOURCE_DIR}/src/FMI_search.h" fmi_header)
mem2_seam(fmi_header "    int64_t sentinel_index;\nprivate:"
    "    int64_t sentinel_index;\n    const int64_t *wgse_count() const { return count; }\n    const CP_OCC *wgse_cp_occ() const { return cp_occ; }\nprivate:")
file(WRITE "${mem2_src}/FMI_search.h" "${fmi_header}")
file(READ "${mem2_SOURCE_DIR}/src/FMI_search.cpp" fmi_source)
mem2_seam(fmi_source "(CP_OCC *)_mm_malloc(cp_occ_size * sizeof(CP_OCC), 64)) == NULL"
    "(CP_OCC *)wgse_metal_index_alloc(cp_occ_size * sizeof(CP_OCC))) == NULL")
file(WRITE "${mem2_src}/FMI_search.cpp" "#include \"metal_backend.h\"\n${fmi_source}")
file(READ "${mem2_SOURCE_DIR}/src/bwamem.cpp" bwamem_source)
include(extension_pipeline.cmake)
mem2_seam(bwamem_source "    kt_for(worker_bwt, &w, n_); // SMEMs (+SAL)"
    "    wgse_kt_for_bwt(w, n_); // SMEMs (+SAL)")
mem2_seam(bwamem_source "    matchArray = mem_collect_smem(fmi, opt,"
    "    if (SMEM *gpu = wgse_metal_smems(fmi, opt, seq_, nseq, mmc, num_smem, tid)) matchArray = gpu;\n    else matchArray = mem_collect_smem(fmi, opt,")
mem2_seam(bwamem_source "    kt_for(worker_sam, &w,  n_);   // SAM"
    "    if (!wgse_metal_pair(opt, w.fmi->idx->bns, w.fmi->idx->pac, pes, n, seqs, w.regs,\n        w.nthreads, wgse_worker_sam_pair, &w))\n        kt_for(worker_sam, &w,  n_);   // SAM")
# worker_sam()'s paired-end loop body for one pair.
string(APPEND bwamem_source "
void wgse_worker_sam_pair(void *data, long pair, int)
{
    worker_t *w = (worker_t*) data;
    long i = pair << 1;
    mem_sam_pe(w->opt, w->fmi->idx->bns, w->fmi->idx->pac, w->pes,
               (w->n_processed >> 1) + pair, &w->seqs[i], &w->regs[i]);
    free(w->regs[i].a);
    free(w->regs[i+1].a);
}

// kt_for(worker_bwt)'s units (BATCH_SIZE reads each), with SMEMs from the GPU.
struct wgse_bwt_t { worker_t *w; int n; };
static void wgse_worker_bwt(void *data, long unit, int tid)
{
    wgse_bwt_t *b = (wgse_bwt_t*) data;
    int st = unit * BATCH_SIZE;
    worker_bwt(b->w, st, b->n - st < BATCH_SIZE ? b->n - st : BATCH_SIZE, tid);
}
void wgse_kt_for_bwt(worker_t &w, int n)
{
    wgse_bwt_t b = {&w, n};
    if (!wgse_metal_seed(w.fmi, w.opt, w.seqs, n, w.nthreads, wgse_worker_bwt, &b))
        kt_for(worker_bwt, &w, n);
}
")
# mem_process_seqs() in two halves, so that with Metal the pipeline maps chunk
# i + 1 while chunk i pairs (mostly GPU mate rescue). Each half is upstream's
# code on the chunk's own alignments, so its output does not depend on overlap.
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/mem_stages.cpp.in" mem_stages)
string(APPEND bwamem_source "${mem_stages}")
file(WRITE "${mem2_src}/bwamem.cpp"
    "#include \"metal_backend.h\"\n#include \"rescue_backend.h\"\n${bwamem_source}")
file(READ "${mem2_SOURCE_DIR}/src/bwamem_pair.cpp" pair_source)
set(rescue_call "aln = ksw_align2(l_ms, seq, re - rb, ref, 5,\n                             opt->mat, opt->o_del, opt->e_del,\n                             opt->o_ins, opt->e_ins, xtra, 0);")
mem2_seam(pair_source "${rescue_call}"
    "if (!wgse_metal_matesw(opt, ms, seq, l_ms, ref, is_rev, rb, re, xtra, &aln))\n                ${rescue_call}")
file(READ "${bwa_dir}/rescue_oracle.c.in" rescue_oracle)
file(WRITE "${mem2_src}/bwamem_pair.cpp"
    "#include \"rescue_backend.h\"\n${pair_source}\n${rescue_oracle}")
file(READ "${mem2_SOURCE_DIR}/src/fastmap.cpp" fastmap_source)
string(REGEX REPLACE
    "void __cpuid\\(unsigned int i, unsigned int cpuid\\[4\\]\\) \\{[^}]+\\}"
    "void __cpuid(unsigned int, unsigned int cpuid[4]) {\n    memset(cpuid, 0, 4 * sizeof(unsigned int));\n}"
    fastmap_source "${fastmap_source}")
# Upstream ignores -o write/close errors and always returns 0.
set(output_close "    if (is_o) {\n        fclose(aux.fp);\n    }\n")
set(final_return "    display_stats(nt);\n\n    return 0;\n}")
string(FIND "${fastmap_source}" "${output_close}" close_at)
string(FIND "${fastmap_source}" "${final_return}" return_at)
if(close_at EQUAL -1 OR return_at EQUAL -1)
    message(FATAL_ERROR "Unexpected upstream BWA-MEM2 output handling")
endif()
string(REPLACE "${output_close}"
    "    int wgse_output_status = 0;\n    if (is_o) {\n        int write_error = ferror(aux.fp);\n        if (fclose(aux.fp) != 0 || write_error) {\n            fprintf(stderr, \"[E::main_mem] failed to write output file\\n\");\n            wgse_output_status = 1;\n        }\n    }\n"
    fastmap_source "${fastmap_source}")
string(REPLACE "${final_return}" "    display_stats(nt);\n\n    return wgse_output_status;\n}"
    fastmap_source "${fastmap_source}")
set(process_call "            mem_process_seqs(opt,
                             aux->n_processed,
                             ret->n_seqs,
                             ret->seqs,
                             aux->pes0,
                             w);")
mem2_seam(fastmap_source "${process_call}" "            if (wgse_overlap)
                wgse_mapped = wgse_mem_map(opt, aux->n_processed, ret->n_seqs, ret->seqs,
                                           aux->pes0, w);
            else
${process_call}")
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/fastmap_stages.cpp.in" fastmap_stages)
mem2_seam(fastmap_source "static void *ktp_worker(void *data)"
    "${fastmap_stages}\nstatic void *ktp_worker(void *data)")
mem2_seam(fastmap_source "        w->data = kt_pipeline(p->shared,"
    "        w->data = wgse_kt_pipeline(p->shared,")
mem2_seam(fastmap_source "    int n_steps = 3;\n"
    "    int n_steps = 3;\n    wgse_overlap = p_nt == 2 && (opt->flag & MEM_F_PE)\n        && !(opt->flag & MEM_F_SMARTPE) && wgse_metal_overlap();\n    if (wgse_overlap) n_steps = 4, p_nt = 3;\n")
# wgse_mapped: step 1's wgse_mem_map() result, taken by the same worker right after it.
file(WRITE "${mem2_src}/fastmap.cpp" "#include \"metal_backend.h\"
#include \"rescue_backend.h\"
static bool wgse_overlap = false;
static void *wgse_mapped = 0;
${fastmap_source}")
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/extension.metal" kernel_source)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/extension_tiled.metal" tiled_source)
string(APPEND kernel_source "\n${tiled_source}")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/extension.metal"
    "${CMAKE_CURRENT_SOURCE_DIR}/extension_tiled.metal")
file(READ "${bwa_dir}/rescue_types.h" rescue_types)
file(READ "${bwa_dir}/rescue.metal" rescue_source)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/seeding_types.h" seeding_types)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/seeding.metal" seeding_source)
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
target_link_libraries(metal_backend PUBLIC mem2_core wgse_metal_common)
add_executable(wgse-bwa-mem2 main.cpp)
target_link_libraries(wgse-bwa-mem2 PRIVATE metal_backend)
set(WGSE_BWA_MEM2_VERSION 0.2.0)
target_compile_definitions(wgse-bwa-mem2 PRIVATE WGSE_VERSION="${WGSE_BWA_MEM2_VERSION}")
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
    add_test(NAME bwa_mem2_rescue_parity
        COMMAND "${CMAKE_CURRENT_SOURCE_DIR}/tests/rescue_parity.sh"
        $<TARGET_FILE:wgse-bwa-mem2> "${CMAKE_CURRENT_BINARY_DIR}/rescue-parity")
    add_test(NAME bwa_mem2_seed_parity
        COMMAND "${CMAKE_CURRENT_SOURCE_DIR}/tests/seed_parity.sh"
        $<TARGET_FILE:wgse-bwa-mem2> "${CMAKE_CURRENT_BINARY_DIR}/seed-parity")
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
install(FILES "${CMAKE_CURRENT_SOURCE_DIR}/../../LICENSE" DESTINATION share/licenses/wgse-bwa-mem2
    RENAME WGSE-LICENSE)
install(FILES "${mem2_SOURCE_DIR}/LICENSE" DESTINATION share/licenses/wgse-bwa-mem2
    RENAME BWA-MEM2-LICENSE)
install(FILES "${neon_SOURCE_DIR}/LICENSE" DESTINATION share/licenses/wgse-bwa-mem2
    RENAME sse2neon-LICENSE)
install(FILES "${safe_SOURCE_DIR}/LICENSE" DESTINATION share/licenses/wgse-bwa-mem2
    RENAME safestringlib-LICENSE)
