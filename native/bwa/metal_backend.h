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
void wgse_set_gpu_locate(bool enable);
// Rows one GPU batch may locate; reads beyond it use upstream bwt_sa().
void wgse_set_gpu_locate_rows(size_t rows);
void wgse_set_gpu_rescue(bool enable);
void wgse_set_gpu_rescue_batch(size_t pairs);
void wgse_inject_rescue_failure();
// Also run each GPU rescue alignment on the CPU and count differences.
void wgse_verify_gpu_rescue();
#endif
