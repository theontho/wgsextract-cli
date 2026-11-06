#pragma once

#include "bwamem.h"
#include "ksw.h"
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Seams patched into the generated copies of upstream bwt.c, bwamem.c and bwamem_pair.c.
// All are inert (upstream behaviour) unless the Metal backend is enabled.
int wgse_metal_enabled(void);
void *wgse_metal_calloc(size_t count, size_t size);
int wgse_metal_map(const mem_opt_t *opt, const bwt_t *bwt, int n, bseq1_t *seqs,
    void (*worker)(void *, long, int), void *data);
int wgse_metal_take(const uint8_t *seq, bwtintv_v *out);
// bwt_sa() for mem_chain(): the GPU's position when it located this row.
bwtint_t wgse_metal_sa(const bwt_t *bwt, bwtint_t k);
// mem_process_seqs() in two halves (appended to the generated bwamem.c), which
// the generated fastmap.c pipelines with Metal: map returns the state pair consumes.
void *wgse_mem_map(const mem_opt_t *opt, const bwt_t *bwt, const bntseq_t *bns, const uint8_t *pac,
    int64_t n_processed, int n, bseq1_t *seqs, const mem_pestat_t *pes0);
void wgse_mem_pair(void *mapped);
// Replaces worker2's kt_for for paired-end chunks: mate rescue on the GPU too.
int wgse_metal_pair(const mem_opt_t *opt, const bntseq_t *bns, const uint8_t *pac,
    const mem_pestat_t pes[4], int n, bseq1_t *seqs, mem_alnreg_v *regs,
    void (*worker)(void *, long, int), void *data);
// mem_matesw()'s ksw_align2(): sets *aln and returns 1 if the GPU aligned
// exactly these inputs for the pair this thread is finishing.
int wgse_metal_matesw(const mem_opt_t *opt, const uint8_t *ms, uint8_t *seq, int l_ms,
    uint8_t *ref, int is_rev, int64_t rb, int64_t re, int xtra, kswr_t *aln);

// Appended to the generated bwamem_pair.c: reports each mate-rescue alignment
// mem_sam_pe() may run for the pair, with mem_matesw()'s inputs.
typedef void (*wgse_rescue_emit_f)(void *ctx, const uint8_t *ms, int l_ms, int is_rev,
    int64_t rb, int64_t re, const uint8_t *ref, int xtra);
void wgse_rescue_candidates(const mem_opt_t *opt, const bntseq_t *bns, const uint8_t *pac,
    const mem_pestat_t pes[4], const bseq1_t s[2], const mem_alnreg_v a[2],
    wgse_rescue_emit_f emit, void *ctx);

// mem_chain2aln()'s ksw_extend2(): sets the outputs and returns 1 if the GPU
// extended exactly these inputs for the pair this thread is finishing.
int wgse_metal_extend(int qlen, const uint8_t *query, int tlen, const uint8_t *target, int w,
    int end_bonus, int h0, int *qle, int *tle, int *gtle, int *gscore, int *max_off, int *score);

// Appended to the generated bwamem.c: worker1() in two stages. The first chains
// the reads of pair (or single-end read) i and reports, for each chain, the
// highest-scoring seed mem_chain2aln() extends first, its reference window
// rseq[0, window) and the seed's offset in it. It returns the chains (or null
// if out of memory), which the second stage aligns and frees.
typedef void (*wgse_extend_emit_f)(void *ctx, int mate, const uint8_t *query, int l_query,
    int qbeg, int len, const uint8_t *rseq, int64_t seed, int64_t window);
void *wgse_worker1_chain(void *data, long i, int tid, wgse_extend_emit_f emit, void *ctx);
void wgse_worker1_finish(void *data, long i, void *chains);
// bns_fetch_seq() for mem_chain2aln(): the window wgse_worker1_chain() fetched
// for this chain, when wgse_worker1_finish() is aligning it.
uint8_t *wgse_fetch_window(const bntseq_t *bns, const uint8_t *pac, int64_t *beg, int64_t mid,
    int64_t *end, int *rid);

// Upstream main(), renamed in the generated main.c.
int wgse_bwa_main(int argc, char *argv[]);

#ifdef __cplusplus
}

#include <string>

enum class Backend { cpu, metal, automatic };

bool wgse_metal_available(std::string &reason);
bool wgse_metal_hardware_supported();
// Must precede alignment; selecting metal or auto requires an available device.
void wgse_set_backend(Backend backend);
bool wgse_metal_execution_failed(std::string &reason);
// Prints GPU/CPU read counts to stderr and releases the GPU buffers.
void wgse_finish_backend();
// Test-only knobs, exposed on the command line only when BUILD_TESTING is on.
// all: every eligible read is prepared on the GPU (CPU threads wait for it).
void wgse_set_gpu_policy(bool all);
void wgse_set_gpu_batch(size_t reads);
void wgse_inject_gpu_failure();
void wgse_inject_locate_failure();
// Reports the first commit of every nth piece of GPU work as interrupted by
// macOS, so it is committed again.
void wgse_inject_gpu_interruptions(size_t every);
void wgse_set_gpu_locate(bool enable);
void wgse_set_gpu_locate_dense(unsigned shift);
void wgse_set_gpu_locate_dense_after(size_t reads);
// Rows one GPU batch may locate; reads beyond it use upstream bwt_sa().
void wgse_set_gpu_locate_rows(size_t rows);
void wgse_set_gpu_rescue(bool enable);
void wgse_set_gpu_rescue_batch(size_t pairs);
void wgse_inject_rescue_failure();
// Also run each GPU rescue alignment on the CPU and count differences.
void wgse_verify_gpu_rescue();
void wgse_set_gpu_extend(bool enable);
// Pairs per GPU extension batch.
void wgse_set_gpu_extend_pairs(size_t pairs);
// GPU extension batches; when none is free, CPU threads extend themselves.
void wgse_set_gpu_extend_batches(size_t batches);
// Pairs across all extension batches, which bound their memory (about 12 KiB each).
size_t wgse_gpu_extend_pool_pairs();
void wgse_inject_extend_failure();
// Also run each GPU seed extension on the CPU and count differences.
void wgse_verify_gpu_extend();
#endif
