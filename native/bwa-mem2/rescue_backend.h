#pragma once

// Seams for GPU SMEM collection, GPU mate rescue and mapping one chunk while
// the previous one pairs, in the generated bwamem.cpp, bwamem_pair.cpp and fastmap.cpp.
#include "bwamem.h"

// Replaces kt_for(worker_bwt) (appended to bwamem.cpp).
void wgse_kt_for_bwt(worker_t &w, int n);
// Runs worker(data, unit, tid) for each unit of BATCH_SIZE reads, collecting
// their SMEMs on the GPU too; returns 0 to leave the chunk to upstream.
int wgse_metal_seed(FMI_search *fmi, const mem_opt_t *opt, bseq1_t *seqs, int n, int threads,
    void (*worker)(void *, long, int), void *data);
// mem_collect_smem()'s result for these reads from the GPU, in mmc's matchArray
// for tid, or nullptr to leave them to mem_collect_smem().
SMEM *wgse_metal_smems(FMI_search *fmi, const mem_opt_t *opt, const bseq1_t *seq, int nseq,
    mem_cache *mmc, int64_t &total, int tid);
SMEM *mem_collect_smem(FMI_search *fmi, const mem_opt_t *opt, const bseq1_t *seq_, int nseq,
    SMEM *matchArray, int32_t *min_intv_ar, int16_t *query_pos_ar, uint8_t *enc_qdb,
    int32_t *rid, mem_cache *mmc, int64_t &tot_smem, int tid);

// mem_process_seqs() in two halves (mem_stages.cpp.in).
void *wgse_mem_map(mem_opt_t *opt, int64_t n_processed, int n, bseq1_t *seqs,
    const mem_pestat_t *pes0, worker_t &w);
void wgse_mem_pair(void *mapped);

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
