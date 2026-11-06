#pragma once

#include "bwamem.h"
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

// Seams patched into the generated copies of upstream bwt.c and bwamem.c.
// All are inert (upstream behaviour) unless the Metal backend is enabled.
int wgse_metal_enabled(void);
void *wgse_metal_calloc(size_t count, size_t size);
int wgse_metal_map(const mem_opt_t *opt, const bwt_t *bwt, int n, bseq1_t *seqs,
    void (*worker)(void *, long, int), void *data);
int wgse_metal_take(const uint8_t *seq, bwtintv_v *out);
// bwt_sa() for mem_chain(): the GPU's position when it located this row.
bwtint_t wgse_metal_sa(const bwt_t *bwt, bwtint_t k);

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
#endif
