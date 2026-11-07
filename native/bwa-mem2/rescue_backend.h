#pragma once

// Seams for GPU mate rescue in the generated bwamem.cpp and bwamem_pair.cpp.
#include "bwamem.h"

// Replaces worker_sam's kt_for for paired-end chunks: mate rescue on the GPU
// too. worker(data, pair, tid) finishes one pair; returns 0 to leave the chunk
// to upstream.
int wgse_metal_pair(const mem_opt_t *opt, const bntseq_t *bns, const uint8_t *pac,
    const mem_pestat_t pes[4], int n, bseq1_t *seqs, mem_alnreg_v *regs, int threads,
    void (*worker)(void *, long, int), void *data);
// mem_matesw()'s ksw_align2(): sets *aln and returns 1 if the GPU aligned
// exactly these inputs, otherwise returns 0.
int wgse_metal_matesw(const mem_opt_t *opt, const uint8_t *ms, uint8_t *seq, int l_ms,
    uint8_t *ref, int is_rev, int64_t rb, int64_t re, int xtra, kswr_t *aln);
// worker_sam()'s work for one pair (appended to bwamem.cpp).
void wgse_worker_sam_pair(void *data, long pair, int tid);
// Appended to bwamem_pair.cpp (native/bwa/rescue_oracle.c.in): reports each
// mate-rescue alignment mem_sam_pe() may run for the pair, with mem_matesw()'s inputs.
typedef void (*wgse_rescue_emit_f)(void *ctx, const uint8_t *ms, int l_ms, int is_rev,
    int64_t rb, int64_t re, const uint8_t *ref, int xtra);
void wgse_rescue_candidates(const mem_opt_t *opt, const bntseq_t *bns, const uint8_t *pac,
    const mem_pestat_t pes[4], const bseq1_t s[2], const mem_alnreg_v a[2],
    wgse_rescue_emit_f emit, void *ctx);
