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
    "${CMAKE_CURRENT_SOURCE_DIR}/seed_oracle.c.in" "${CMAKE_CURRENT_SOURCE_DIR}/rescue_oracle.c.in"
    "${CMAKE_CURRENT_SOURCE_DIR}/extend_oracle.c.in" "${CMAKE_CURRENT_SOURCE_DIR}/mem_stages.c.in"
    "${CMAKE_CURRENT_SOURCE_DIR}/sort_oracle.c.in" "${CMAKE_CURRENT_SOURCE_DIR}/pac_oracle.c.in"
    "${CMAKE_CURRENT_SOURCE_DIR}/ksw_oracle.c.in"
    "${CMAKE_CURRENT_SOURCE_DIR}/fastmap_stages.c.in")
file(READ "${bwa_SOURCE_DIR}/bwamem.c" mem_source)
seam_replace(mem_source "mem_collect_intv(opt, bwt, len, seq, aux);"
    "if (!wgse_metal_take(opt, bwt, seq, &aux->mem)) {\n\t\tmem_collect_intv(opt, bwt, len, seq, aux);\n\t\twgse_metal_locate(opt, bwt, &aux->mem);\n\t}")
seam_replace(mem_source
    "kt_for(opt->n_threads, worker1, &w, (opt->flag&MEM_F_PE)? n>>1 : n);"
    "if (!wgse_metal_map(opt, bwt, n, seqs, worker1, &w))\n\t\tkt_for(opt->n_threads, worker1, &w, (opt->flag&MEM_F_PE)? n>>1 : n);")
seam_replace(mem_source
    "kt_for(opt->n_threads, worker2, &w, (opt->flag&MEM_F_PE)? n>>1 : n);"
    "if (!wgse_metal_pair(opt, bns, pac, pes, n, seqs, w.regs, worker2, &w))\n\t\tkt_for(opt->n_threads, worker2, &w, (opt->flag&MEM_F_PE)? n>>1 : n);")
seam_replace(mem_source "s.rbeg = tmp.pos = bwt_sa(bwt, p->x[0] + k);"
    "s.rbeg = tmp.pos = wgse_metal_sa(bwt, p->x[0] + k);")
seam_replace(mem_source "rseq = bns_fetch_seq(bns, pac, &rmax[0], c->seeds[0].rbeg, &rmax[1], &rid);"
    "rseq = wgse_fetch_window(bns, pac, &rmax[0], c->seeds[0].rbeg, &rmax[1], &rid);")
# mem_chain2aln()'s left and right extensions: the GPU's result if it extended
# exactly these inputs, otherwise upstream's.
set(left_call "a->score = ksw_extend2(s->qbeg, qs, tmp, rs, 5, opt->mat, opt->o_del, opt->e_del, opt->o_ins, opt->e_ins, aw[0], opt->pen_clip5, opt->zdrop, s->len * opt->a, &qle, &tle, &gtle, &gscore, &max_off[0]);")
seam_replace(mem_source "${left_call}"
    "if (!wgse_metal_extend(s->qbeg, qs, tmp, rs, aw[0], opt->pen_clip5, s->len * opt->a, &qle, &tle, &gtle, &gscore, &max_off[0], &a->score))\n\t\t\t\t\t${left_call}")
set(right_call "a->score = ksw_extend2(l_query - qe, query + qe, rmax[1] - rmax[0] - re, rseq + re, 5, opt->mat, opt->o_del, opt->e_del, opt->o_ins, opt->e_ins, aw[1], opt->pen_clip3, opt->zdrop, sc0, &qle, &tle, &gtle, &gscore, &max_off[1]);")
seam_replace(mem_source "${right_call}"
    "if (!wgse_metal_extend(l_query - qe, query + qe, rmax[1] - rmax[0] - re, rseq + re, aw[1], opt->pen_clip3, sc0, &qle, &tle, &gtle, &gscore, &max_off[1], &a->score))\n\t\t\t\t\t${right_call}")
# mem_sort_dedup_patch()'s sorts move pointers instead of regions (sort_oracle.c.in).
seam_replace(mem_source "ks_introsort(mem_ars2, n, a); // sort by the END position, not START!"
    "if (wgse_metal_enabled()) wgse_sort_alnreg_by_end(n, a);\n\telse ks_introsort(mem_ars2, n, a);")
seam_replace(mem_source "\tks_introsort(mem_ars, n, a);\n\tfor (i = 1; i < n; ++i) { // mark identical hits"
    "\tif (wgse_metal_enabled()) wgse_sort_alnreg_by_score(n, a);\n\telse ks_introsort(mem_ars, n, a);\n\tfor (i = 1; i < n; ++i) { // mark identical hits")
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/seed_oracle.c.in" oracle_source)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/extend_oracle.c.in" extend_oracle_source)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/mem_stages.c.in" mem_stages_source)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/sort_oracle.c.in" sort_oracle_source)
file(WRITE "${generated}/bwamem.c"
    "#include \"metal_backend.h\"\n${mem_source}\n${oracle_source}\n${extend_oracle_source}\n${mem_stages_source}\n${sort_oracle_source}")

file(READ "${bwa_SOURCE_DIR}/bwamem_pair.c" pair_source)
set(rescue_call "aln = ksw_align2(l_ms, seq, re - rb, ref, 5, opt->mat, opt->o_del, opt->e_del, opt->o_ins, opt->e_ins, xtra, 0);")
seam_replace(pair_source "${rescue_call}"
    "if (!wgse_metal_matesw(opt, ms, seq, l_ms, ref, is_rev, rb, re, xtra, &aln))\n\t\t\t\t${rescue_call}")
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/rescue_oracle.c.in" rescue_source)
file(WRITE "${generated}/bwamem_pair.c"
    "#include \"metal_backend.h\"\n${pair_source}\n${rescue_source}")

# bns_get_seq() unpacks a byte of bases at a time (pac_oracle.c.in).
file(READ "${bwa_SOURCE_DIR}/bntseq.c" bntseq_source)
seam_replace(bntseq_source "\t\t\tfor (k = end_f; k > beg_f; --k)\n\t\t\t\tseq[l++] = 3 - _get_pac(pac, k);"
    "\t\t\tif (wgse_metal_enabled()) wgse_unpack_pac_reverse(pac, beg_f, end_f, seq);\n\t\t\telse for (k = end_f; k > beg_f; --k)\n\t\t\t\tseq[l++] = 3 - _get_pac(pac, k);")
seam_replace(bntseq_source "\t\t\tfor (k = beg; k < end; ++k)\n\t\t\t\tseq[l++] = _get_pac(pac, k);"
    "\t\t\tif (wgse_metal_enabled()) wgse_unpack_pac(pac, beg, end, seq);\n\t\t\telse for (k = beg; k < end; ++k)\n\t\t\t\tseq[l++] = _get_pac(pac, k);")
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/pac_oracle.c.in" pac_oracle_source)
file(WRITE "${generated}/bntseq.c" "#include \"metal_backend.h\"\n${bntseq_source}\n${pac_oracle_source}")
# Batched ksw_align2() for CPU threads' mate rescue (ksw_oracle.c.in); upstream code is unchanged.
file(READ "${bwa_SOURCE_DIR}/ksw.c" ksw_source)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/ksw_oracle.c.in" ksw_oracle_source)
file(WRITE "${generated}/ksw.c" "#include \"metal_backend.h\"\n${ksw_source}\n${ksw_oracle_source}")

# Metal shares the BWT and the restored (not the index-time) suffix array in
# place, which needs page-aligned storage.
file(READ "${bwa_SOURCE_DIR}/bwt.c" bwt_source)
seam_replace(bwt_source "calloc(bwt->bwt_size, 4)"
    "(wgse_metal_enabled() ? wgse_metal_calloc(bwt->bwt_size, 4) : calloc(bwt->bwt_size, 4))")
seam_replace(bwt_source
    "bwt->sa = (bwtint_t*)calloc(bwt->n_sa, sizeof(bwtint_t));\n\tbwt->sa[0] = -1;"
    "bwt->sa = (bwtint_t*)(wgse_metal_enabled() ? wgse_metal_calloc(bwt->n_sa, sizeof(bwtint_t)) : calloc(bwt->n_sa, sizeof(bwtint_t)));\n\tbwt->sa[0] = -1;")
file(WRITE "${generated}/bwt.c" "#include \"metal_backend.h\"\n${bwt_source}")

# With Metal, map the next chunk while this one pairs (fastmap_stages.c.in).
file(READ "${bwa_SOURCE_DIR}/fastmap.c" fastmap_source)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/fastmap_stages.c.in" fastmap_stages_source)
seam_replace(fastmap_source "int main_mem(int argc, char *argv[])"
    "${fastmap_stages_source}int main_mem(int argc, char *argv[])")
seam_replace(fastmap_source "kt_pipeline(no_mt_io? 1 : 2, process, &aux, 3);"
    "if (!no_mt_io && !(opt->flag & MEM_F_SMARTPE) && wgse_metal_enabled())\n\t\tkt_pipeline(3, wgse_process, &aux, 4);\n\telse kt_pipeline(no_mt_io? 1 : 2, process, &aux, 3);")
file(WRITE "${generated}/fastmap.c" "#include \"metal_backend.h\"\n${fastmap_source}")

file(READ "${bwa_SOURCE_DIR}/main.c" main_source)
seam_replace(main_source "int main(int argc, char *argv[])"
    "int wgse_bwa_main(int argc, char *argv[])")
# Keep upstream's "[main] Version/CMD/Real time" stderr lines.
seam_replace(main_source "__func__" "\"main\"")
file(WRITE "${generated}/main.c" "${main_source}")
set(patched_sources bwamem bwamem_pair bwt bntseq ksw fastmap main)
