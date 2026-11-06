# Generate copies of pinned upstream sources with narrow Metal seams. Each seam
# is inert unless the Metal backend is enabled, so the CPU path stays upstream.
function(seam_replace variable before after)
    string(FIND "${${variable}}" "${before}" position)
    if(position EQUAL -1)
        message(FATAL_ERROR "Unexpected upstream BWA source: ${before}")
    endif()
    string(REPLACE "${before}" "${after}" patched "${${variable}}")
    set(${variable} "${patched}" PARENT_SCOPE)
endfunction()

set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/seed_oracle.c.in" "${CMAKE_CURRENT_SOURCE_DIR}/rescue_oracle.c.in")
file(READ "${bwa_SOURCE_DIR}/bwamem.c" mem_source)
seam_replace(mem_source "mem_collect_intv(opt, bwt, len, seq, aux);"
    "if (!wgse_metal_take(seq, &aux->mem)) mem_collect_intv(opt, bwt, len, seq, aux);")
seam_replace(mem_source
    "kt_for(opt->n_threads, worker1, &w, (opt->flag&MEM_F_PE)? n>>1 : n);"
    "if (!wgse_metal_map(opt, bwt, n, seqs, worker1, &w))\n\t\tkt_for(opt->n_threads, worker1, &w, (opt->flag&MEM_F_PE)? n>>1 : n);")
seam_replace(mem_source
    "kt_for(opt->n_threads, worker2, &w, (opt->flag&MEM_F_PE)? n>>1 : n);"
    "if (!wgse_metal_pair(opt, bns, pac, pes, n, seqs, w.regs, worker2, &w))\n\t\tkt_for(opt->n_threads, worker2, &w, (opt->flag&MEM_F_PE)? n>>1 : n);")
seam_replace(mem_source "s.rbeg = tmp.pos = bwt_sa(bwt, p->x[0] + k);"
    "s.rbeg = tmp.pos = wgse_metal_sa(bwt, p->x[0] + k);")
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/seed_oracle.c.in" oracle_source)
file(WRITE "${generated}/bwamem.c"
    "#include \"metal_backend.h\"\n${mem_source}\n${oracle_source}")

file(READ "${bwa_SOURCE_DIR}/bwamem_pair.c" pair_source)
set(rescue_call "aln = ksw_align2(l_ms, seq, re - rb, ref, 5, opt->mat, opt->o_del, opt->e_del, opt->o_ins, opt->e_ins, xtra, 0);")
seam_replace(pair_source "${rescue_call}"
    "if (!wgse_metal_matesw(opt, ms, seq, l_ms, ref, is_rev, rb, re, xtra, &aln))\n\t\t\t\t${rescue_call}")
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/rescue_oracle.c.in" rescue_source)
file(WRITE "${generated}/bwamem_pair.c"
    "#include \"metal_backend.h\"\n${pair_source}\n${rescue_source}")

# Metal shares the BWT and the restored (not the index-time) suffix array in
# place, which needs page-aligned storage.
file(READ "${bwa_SOURCE_DIR}/bwt.c" bwt_source)
seam_replace(bwt_source "calloc(bwt->bwt_size, 4)"
    "(wgse_metal_enabled() ? wgse_metal_calloc(bwt->bwt_size, 4) : calloc(bwt->bwt_size, 4))")
seam_replace(bwt_source
    "bwt->sa = (bwtint_t*)calloc(bwt->n_sa, sizeof(bwtint_t));\n\tbwt->sa[0] = -1;"
    "bwt->sa = (bwtint_t*)(wgse_metal_enabled() ? wgse_metal_calloc(bwt->n_sa, sizeof(bwtint_t)) : calloc(bwt->n_sa, sizeof(bwtint_t)));\n\tbwt->sa[0] = -1;")
file(WRITE "${generated}/bwt.c" "#include \"metal_backend.h\"\n${bwt_source}")

file(READ "${bwa_SOURCE_DIR}/main.c" main_source)
seam_replace(main_source "int main(int argc, char *argv[])"
    "int wgse_bwa_main(int argc, char *argv[])")
# Keep upstream's "[main] Version/CMD/Real time" stderr lines.
seam_replace(main_source "__func__" "\"main\"")
file(WRITE "${generated}/main.c" "${main_source}")
set(patched_sources bwamem bwamem_pair bwt main)
